"""Auto-suggest mappings to cut first-use setup effort.

Deterministic, explainable similarity scoring (no heavy ML dependency):
token normalization, Jaccard overlap, difflib sequence similarity,
leaf-segment weighting for CCC hierarchies, and a synonym dictionary for
common network-engineering vocabulary. Every suggestion carries a 0-1
confidence; the UI presents them for review — they are never auto-saved.
"""

import re
from difflib import SequenceMatcher
from typing import Any

SITE_THRESHOLD = 0.4
VARIABLE_THRESHOLD = 0.35

_STOPWORDS = {"global", "site", "sites", "area", "the", "of"}

# variable-name token -> tokens as they appear in NetBox paths
_SYNONYMS: dict[str, list[str]] = {
    "hostname": ["name"],
    "sysname": ["name"],
    "serial": ["serial"],
    "serialnumber": ["serial"],
    "ip": ["primary", "ip4", "address"],
    "ipaddress": ["primary", "ip4", "address"],
    "mgmt": ["primary", "ip4", "address"],
    "management": ["primary", "ip4", "address"],
    "address": ["address"],
    "site": ["site", "name"],
    "location": ["location", "site"],
    "contact": ["contact"],
    "vlan": ["vlan", "vid"],
}


def _tokens(text: str, *, drop_stopwords: bool = True) -> list[str]:
    parts = re.split(r"[^a-z0-9]+", text.lower())
    stopwords = _STOPWORDS if drop_stopwords else set()
    return [p for p in parts if p and p not in stopwords]


def _similarity(a: list[str], b: list[str]) -> float:
    """Blend of token-set overlap (with prefix/abbreviation credit) and
    character-level sequence similarity."""
    if not a or not b:
        return 0.0
    matched = 0
    for token in a:
        for other in b:
            if token == other or (
                min(len(token), len(other)) >= 3
                and (token.startswith(other) or other.startswith(token))
            ):
                matched += 1
                break
    coverage = matched / len(a)
    ratio = SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()
    return max(coverage, ratio)


def suggest_site_mappings(
    netbox_sites: list[dict[str, Any]],
    ccc_sites: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Best unique NetBox→CCC site pairing above SITE_THRESHOLD.

    NetBox name/slug tokens are compared against the full CCC hierarchy and,
    with more weight, its leaf segment. Pairs are assigned greedily, best
    score first, each side used at most once.
    """
    scored: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for netbox in netbox_sites:
        nb_variants = [_tokens(str(netbox.get("name", "")))]
        if netbox.get("slug"):
            nb_variants.append(_tokens(str(netbox["slug"])))
        for ccc in ccc_sites:
            hierarchy = str(ccc.get("name_hierarchy") or ccc.get("siteNameHierarchy") or "")
            segments = [s for s in hierarchy.split("/") if s]
            if not segments:
                continue
            leaf_tokens = _tokens(segments[-1])
            full_tokens = _tokens(hierarchy)
            score = 0.0
            for nb_tokens in nb_variants:
                if not nb_tokens:
                    continue
                if nb_tokens == leaf_tokens:
                    score = max(score, 1.0)
                score = max(
                    score,
                    0.7 * _similarity(nb_tokens, leaf_tokens)
                    + 0.3 * _similarity(nb_tokens, full_tokens),
                )
            if score >= SITE_THRESHOLD:
                scored.append((score, netbox, ccc))

    scored.sort(key=lambda item: item[0], reverse=True)
    used_netbox: set[int] = set()
    used_ccc: set[str] = set()
    suggestions: list[dict[str, Any]] = []
    for score, netbox, ccc in scored:
        if netbox["id"] in used_netbox or ccc["id"] in used_ccc:
            continue
        used_netbox.add(netbox["id"])
        used_ccc.add(ccc["id"])
        hierarchy = str(ccc.get("name_hierarchy") or ccc.get("siteNameHierarchy") or "")
        suggestions.append(
            {
                "netbox_site_id": netbox["id"],
                "netbox_site_name": netbox.get("name", ""),
                "ccc_site_id": ccc["id"],
                "ccc_site_name": hierarchy,
                "confidence": round(score, 2),
            }
        )
    return suggestions


_DEVICE_SCALAR_FIELDS = ("name", "serial", "asset_tag", "description")
_NESTED_NAME_FIELDS = ("site", "role", "device_type", "platform", "tenant", "location")


def candidate_paths(device: dict[str, Any]) -> list[str]:
    """Dot-paths of scalar values usable as Day-N variable sources."""
    paths: list[str] = []
    for field in _DEVICE_SCALAR_FIELDS:
        if isinstance(device.get(field), str | int | float) and device.get(field) != "":
            paths.append(f"device.{field}")
    for field in _NESTED_NAME_FIELDS:
        nested = device.get(field)
        if isinstance(nested, dict) and nested.get("name"):
            paths.append(f"device.{field}.name")
    primary = device.get("primary_ip4")
    if isinstance(primary, dict) and primary.get("address"):
        paths.append("device.primary_ip4.address")
    for container in ("custom_fields", "config_context"):
        data = device.get(container)
        if isinstance(data, dict):
            paths.extend(_flatten(f"device.{container}", data, depth=3))
    return paths


def _flatten(prefix: str, data: dict[str, Any], depth: int) -> list[str]:
    paths: list[str] = []
    for key, value in data.items():
        path = f"{prefix}.{key}"
        if isinstance(value, str | int | float | bool) and value != "":
            paths.append(path)
        elif isinstance(value, dict) and depth > 1:
            paths.extend(_flatten(path, value, depth - 1))
        elif (
            isinstance(value, list)
            and depth > 1
            and value
            and isinstance(value[0], str | int | float)
        ):
            paths.append(f"{path}.0")
    return paths


def _expand(tokens: list[str]) -> list[str]:
    expanded = list(tokens)
    for token in tokens:
        expanded.extend(_SYNONYMS.get(token, []))
    return list(dict.fromkeys(expanded))  # dedupe, keep order


def suggest_variable_mappings(
    variables: list[str],
    device: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Best dot-path per template variable, or None below VARIABLE_THRESHOLD."""
    candidates = [
        (path, _tokens(path.removeprefix("device."), drop_stopwords=False))
        for path in candidate_paths(device)
    ]
    result: dict[str, dict[str, Any]] = {}
    for variable in variables:
        raw_tokens = _tokens(variable, drop_stopwords=False)
        var_tokens = _expand(raw_tokens)
        best_path: str | None = None
        best_score = 0.0
        for path, path_tokens in candidates:
            # F1-style blend: a path must both cover the variable AND be
            # covered by it, so short generic paths can't crowd out
            # specific ones (e.g. device.name vs ...syslog.host).
            precision = _similarity(_expand(path_tokens), var_tokens)
            recall = _similarity(var_tokens, path_tokens)
            if set(path_tokens) <= set(var_tokens):
                # the path is fully explained by the variable (incl. synonyms)
                recall = 1.0
            score = 2 * precision * recall / (precision + recall) if precision and recall else 0.0
            # literal (non-synonym) token hits break ties in favor of the
            # more specific path (SITE -> device.site.name, not device.name)
            score += 0.05 * len(set(path_tokens) & set(raw_tokens))
            if score > best_score:
                best_score = score
                best_path = path
        if best_score < VARIABLE_THRESHOLD:
            best_path = None
        result[variable] = {
            "source_path": best_path,
            "confidence": round(min(best_score, 1.0), 2) if best_path else 0.0,
        }
    return result
