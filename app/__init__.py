"""PnP Bridge backend package."""

from importlib.metadata import PackageNotFoundError, version

# The released version from the package metadata (pyproject.toml), so /api/health
# identifies a running instance. A hard-coded value here stayed at 1.1.0 for
# every release since.
try:
    __version__ = version("pnp-bridge")
except PackageNotFoundError:  # plain source tree without an install
    __version__ = "0+unknown"
