import { useEffect, useState } from 'react'

interface DayNMapping {
  variable: string
  source_path: string
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

const inputClass =
  'block w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm ' +
  'focus:border-sky-500 focus:outline-none dark:border-slate-700 dark:bg-slate-900'

export default function SettingsDayN() {
  const [rows, setRows] = useState<DayNMapping[] | null>(null)
  const [banner, setBanner] = useState<Banner | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    fetchJson<{ mappings: DayNMapping[] }>('/api/settings/dayn')
      .then((body) => setRows(body.mappings))
      .catch((err: Error) => setBanner({ ok: false, detail: err.message }))
  }, [])

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
