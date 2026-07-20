import { useEffect, useState } from 'react'

interface DayNMapping {
  variable: string
  source_path: string
}

interface Template {
  id: string
  name: string
  project: string | null
}

interface DayNSuggestion {
  variable: string
  source_path: string | null
  confidence: number
}

interface TemplateSecret {
  name: string
  secret_masked: string
}

interface PreviewVariable {
  variable: string
  source_path: string | null
  value: string | null
  source: string
}

interface PreviewResult {
  netbox_device_id: number
  netbox_name: string | null
  netbox_site: string | null
  variables: PreviewVariable[]
}

interface Banner {
  ok: boolean
  detail: string
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: string }
      if (body.detail) detail = body.detail
    } catch {
      /* keep status */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

// no width here — each usage sets its own (w-56 / w-72 / flex-1)
const inputClass =
  'block rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm ' +
  'focus:border-sky-500 focus:outline-none dark:border-slate-700 dark:bg-slate-900'

export default function SettingsDayN() {
  const [rows, setRows] = useState<DayNMapping[] | null>(null)
  const [banner, setBanner] = useState<Banner | null>(null)
  const [busy, setBusy] = useState(false)
  const [templates, setTemplates] = useState<Template[]>([])
  const [templateId, setTemplateId] = useState('')
  const [suggesting, setSuggesting] = useState(false)
  const [confidences, setConfidences] = useState<Record<string, number>>({})

  const [secrets, setSecrets] = useState<TemplateSecret[]>([])
  const [newSecretName, setNewSecretName] = useState('')
  const [newSecretValue, setNewSecretValue] = useState('')
  const [secretBusy, setSecretBusy] = useState(false)

  const [previewSerial, setPreviewSerial] = useState('')
  const [preview, setPreview] = useState<PreviewResult | null>(null)
  const [previewing, setPreviewing] = useState(false)

  const runPreview = async () => {
    setPreviewing(true)
    setBanner(null)
    try {
      const result = await fetchJson<PreviewResult>('/api/settings/dayn/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ serial: previewSerial, template_id: templateId || null }),
      })
      setPreview(result)
    } catch (err) {
      setPreview(null)
      setBanner({ ok: false, detail: (err as Error).message })
    } finally {
      setPreviewing(false)
    }
  }

  useEffect(() => {
    fetchJson<{ mappings: DayNMapping[] }>('/api/settings/dayn')
      .then((body) => setRows(body.mappings))
      .catch((err: Error) => setBanner({ ok: false, detail: err.message }))
    fetchJson<Template[]>('/api/wizard/day0/templates')
      .then((list) => setTemplates(Array.isArray(list) ? list : []))
      .catch(() => setTemplates([])) // suggestions simply unavailable
    fetchJson<TemplateSecret[]>('/api/settings/secrets')
      .then((list) => setSecrets(Array.isArray(list) ? list : []))
      .catch((err: Error) => setBanner({ ok: false, detail: err.message }))
  }, [])

  const addSecret = async () => {
    setSecretBusy(true)
    setBanner(null)
    try {
      const saved = await fetchJson<TemplateSecret>(
        `/api/settings/secrets/${encodeURIComponent(newSecretName.trim())}`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ secret: newSecretValue }),
        },
      )
      setSecrets((prev) => [...prev.filter((s) => s.name !== saved.name), saved])
      setNewSecretName('')
      setNewSecretValue('')
      setBanner({
        ok: true,
        detail: `Secret '${saved.name}' stored — it auto-fills any template variable named ${saved.name}`,
      })
    } catch (err) {
      setBanner({ ok: false, detail: (err as Error).message })
    } finally {
      setSecretBusy(false)
    }
  }

  const deleteSecret = async (name: string) => {
    setBanner(null)
    try {
      const res = await fetch(`/api/settings/secrets/${encodeURIComponent(name)}`, {
        method: 'DELETE',
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setSecrets((prev) => prev.filter((s) => s.name !== name))
    } catch (err) {
      setBanner({ ok: false, detail: (err as Error).message })
    }
  }

  const suggest = async () => {
    setSuggesting(true)
    setBanner(null)
    try {
      const suggestions = await fetchJson<DayNSuggestion[]>('/api/settings/dayn/suggest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ template_id: templateId }),
      })
      const nextConfidences: Record<string, number> = {}
      setRows((prev) => {
        const merged = [...(prev ?? [])]
        for (const s of suggestions) {
          const existing = merged.find((r) => r.variable === s.variable)
          if (existing) {
            if (!existing.source_path && s.source_path) {
              existing.source_path = s.source_path
              nextConfidences[s.variable] = s.confidence
            }
          } else {
            merged.push({ variable: s.variable, source_path: s.source_path ?? '' })
            if (s.source_path) nextConfidences[s.variable] = s.confidence
          }
        }
        return merged
      })
      setConfidences((prev) => ({ ...prev, ...nextConfidences }))
      const matched = suggestions.filter((s) => s.source_path).length
      setBanner({
        ok: true,
        detail:
          `Suggested ${matched} of ${suggestions.length} template variables — ` +
          'review, correct where needed, then save. Empty paths stay manual entry.',
      })
    } catch (err) {
      setBanner({ ok: false, detail: (err as Error).message })
    } finally {
      setSuggesting(false)
    }
  }

  const update = (index: number, patch: Partial<DayNMapping>) =>
    setRows((prev) => (prev ?? []).map((row, i) => (i === index ? { ...row, ...patch } : row)))

  const save = async () => {
    setBusy(true)
    setBanner(null)
    try {
      const body = await fetchJson<{ mappings: DayNMapping[] }>('/api/settings/dayn', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mappings: (rows ?? []).filter((r) => r.variable.trim() && r.source_path.trim()),
        }),
      })
      setRows(body.mappings)
      setBanner({ ok: true, detail: 'Mappings saved.' })
    } catch (err) {
      setBanner({ ok: false, detail: (err as Error).message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="max-w-3xl">
      <h1 className="text-2xl font-bold tracking-tight">Day-N Variables</h1>
      <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
        Map Catalyst Center template variables to NetBox data via dot-paths, e.g.{' '}
        <code>device.custom_fields.snmp_location</code> or{' '}
        <code>device.config_context.ntp.servers.0</code>. Unmapped variables become manual input
        fields in wizard step 4.
      </p>

      <section
        aria-label="Template secrets"
        className="mt-6 rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
      >
        <h2 className="font-semibold">Global variables &amp; secrets</h2>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Store keys and passwords (AES encryption key, RADIUS/TACACS keys, SNMP communities, …)
          once, encrypted. A secret{' '}
          <strong>auto-fills any Day-0 or Day-N template variable with the same name</strong> — e.g.
          name it <code>AES_ENCRYPTION_KEY</code> and it fills that variable everywhere — or
          reference it explicitly as <code>secret.&lt;name&gt;</code>. Values are write-only: masked
          everywhere and decrypted only for the deploy/claim call to Catalyst Center.
        </p>
        <ul className="mt-3 flex flex-col gap-1">
          {secrets.map((s) => (
            <li
              key={s.name}
              className="flex items-center justify-between rounded-md border border-slate-200 px-3 py-2 text-sm dark:border-slate-800"
            >
              <span>
                <code className="font-mono">secret.{s.name}</code>
                <span className="ml-3 text-slate-400">{s.secret_masked}</span>
              </span>
              <button
                type="button"
                className="text-rose-600 hover:underline dark:text-rose-400"
                onClick={() => void deleteSecret(s.name)}
              >
                Delete
              </button>
            </li>
          ))}
          {secrets.length === 0 && (
            <li className="text-sm text-slate-400">No template secrets stored yet.</li>
          )}
        </ul>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            aria-label="Secret name"
            className={inputClass + ' w-56 font-mono'}
            placeholder="radius_key"
            value={newSecretName}
            onChange={(e) => setNewSecretName(e.target.value)}
          />
          <input
            aria-label="Secret value"
            type="password"
            className={inputClass + ' w-72'}
            placeholder="value (write-only)"
            value={newSecretValue}
            onChange={(e) => setNewSecretValue(e.target.value)}
          />
          <button
            type="button"
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800"
            disabled={!newSecretName.trim() || !newSecretValue || secretBusy}
            onClick={() => void addSecret()}
          >
            {secretBusy ? 'Storing…' : 'Store secret'}
          </button>
        </div>
      </section>

      <section
        aria-label="Suggest mappings"
        className="mt-6 rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
      >
        <h2 className="font-semibold">Pre-match from a template</h2>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Pick a CCC template and let PnP Bridge match its variables against your NetBox data
          (fields, custom fields, config contexts). Review the suggestions, correct what does not
          fit, then save.
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <label className="block">
            <span className="sr-only">Template for suggestions</span>
            <select
              className={inputClass + ' w-72'}
              value={templateId}
              onChange={(e) => setTemplateId(e.target.value)}
            >
              <option value="">— select template —</option>
              {templates.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.project ? `${t.project} / ` : ''}
                  {t.name}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800"
            disabled={!templateId || suggesting}
            onClick={() => void suggest()}
          >
            {suggesting ? 'Matching…' : 'Suggest mappings'}
          </button>
        </div>
      </section>

      <section
        aria-label="Verify with a device"
        className="mt-6 rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
      >
        <h2 className="font-semibold">Verify against a real device</h2>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Enter a device serial number to resolve the mappings below against the real NetBox data
          for that device — so you can check the values against reality before deploying. Read-only;
          nothing is changed. If a template is selected above, its variables are used; otherwise the
          saved mappings are.
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            aria-label="Device serial to verify"
            className={inputClass + ' w-72 font-mono'}
            placeholder="e.g. FOC21262B0R"
            value={previewSerial}
            onChange={(e) => setPreviewSerial(e.target.value)}
          />
          <button
            type="button"
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800"
            disabled={!previewSerial.trim() || previewing}
            onClick={() => void runPreview()}
          >
            {previewing ? 'Resolving…' : 'Verify'}
          </button>
        </div>
        {preview && (
          <div className="mt-4">
            <p className="text-sm text-slate-600 dark:text-slate-300">
              <strong>{preview.netbox_name ?? '—'}</strong>
              {preview.netbox_site ? ` · ${preview.netbox_site}` : ''}
            </p>
            <div className="mt-2 overflow-x-auto rounded-md border border-slate-200 dark:border-slate-800">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 text-xs text-slate-500 uppercase dark:border-slate-800 dark:text-slate-400">
                  <tr>
                    <th className="px-3 py-2">Variable</th>
                    <th className="px-3 py-2">Source</th>
                    <th className="px-3 py-2">Resolved value</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.variables.map((v) => (
                    <tr
                      key={v.variable}
                      className="border-b border-slate-100 last:border-0 dark:border-slate-800"
                    >
                      <td className="px-3 py-2 font-mono">{v.variable}</td>
                      <td className="px-3 py-2 font-mono text-xs text-slate-500 dark:text-slate-400">
                        {v.source_path || '— manual —'}
                      </td>
                      <td className="px-3 py-2">
                        {v.source === 'manual' ? (
                          <span className="text-amber-600 dark:text-amber-400">manual entry</span>
                        ) : (
                          <span className="font-mono">{v.value}</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </section>

      <div className="mt-6 flex flex-col gap-2">
        {rows === null && !banner && <p className="text-sm text-slate-400">Loading…</p>}
        {(rows ?? []).map((row, index) => (
          <div key={index} className="flex items-center gap-2">
            <input
              aria-label={`Variable ${index + 1}`}
              className={inputClass + ' w-56 font-mono'}
              placeholder="TEMPLATE_VARIABLE"
              value={row.variable}
              onChange={(e) => update(index, { variable: e.target.value })}
            />
            <span className="text-slate-400">→</span>
            <input
              aria-label={`Source path ${index + 1}`}
              className={inputClass + ' flex-1 font-mono'}
              placeholder="device.custom_fields…"
              value={row.source_path}
              onChange={(e) => update(index, { source_path: e.target.value })}
            />
            {confidences[row.variable] !== undefined && (
              <span
                className="rounded bg-sky-100 px-1.5 py-0.5 text-xs whitespace-nowrap text-sky-800 dark:bg-sky-900/40 dark:text-sky-300"
                title="Suggested automatically — review before saving"
              >
                suggested · {Math.round(confidences[row.variable] * 100)}%
              </span>
            )}
            <button
              type="button"
              className="text-rose-600 hover:underline dark:text-rose-400"
              onClick={() => setRows((prev) => (prev ?? []).filter((_, i) => i !== index))}
            >
              Remove
            </button>
          </div>
        ))}
      </div>

      <div className="mt-4 flex items-center gap-3">
        <button
          type="button"
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
          onClick={() => setRows((prev) => [...(prev ?? []), { variable: '', source_path: '' }])}
        >
          Add mapping
        </button>
        <button
          type="button"
          className="rounded-md bg-sky-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-sky-500 disabled:opacity-50"
          disabled={busy || rows === null}
          onClick={() => void save()}
        >
          {busy ? 'Saving…' : 'Save mappings'}
        </button>
        {banner && (
          <p
            role="status"
            className={
              'rounded-md px-3 py-2 text-sm ' +
              (banner.ok
                ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300'
                : 'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300')
            }
          >
            {banner.detail}
          </p>
        )}
      </div>
    </div>
  )
}
