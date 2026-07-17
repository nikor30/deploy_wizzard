import { useCallback, useEffect, useState } from 'react'

interface LogEntry {
  id: number
  timestamp: string
  level: string
  component: string
  message: string
  job_id: number | null
  device_serial: string | null
  context: Record<string, unknown> | null
}

interface WebhookDelivery {
  id: number
  job_id: number
  device_serial: string
  status: string
  attempts: number
  last_error: string | null
  created_at: string
  payload: Record<string, unknown>
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

const LEVEL_STYLES: Record<string, string> = {
  ERROR: 'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300',
  WARNING: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  INFO: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300',
}

const inputClass =
  'rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm ' +
  'focus:border-sky-500 focus:outline-none dark:border-slate-700 dark:bg-slate-900'

export default function Logs() {
  const [entries, setEntries] = useState<LogEntry[]>([])
  const [total, setTotal] = useState(0)
  const [deliveries, setDeliveries] = useState<WebhookDelivery[]>([])
  const [expanded, setExpanded] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [level, setLevel] = useState('')
  const [jobId, setJobId] = useState('')
  const [serial, setSerial] = useState('')
  const [q, setQ] = useState('')

  const load = useCallback(() => {
    const params = new URLSearchParams()
    if (level) params.set('level', level)
    if (jobId) params.set('job_id', jobId)
    if (serial) params.set('serial', serial)
    if (q) params.set('q', q)
    fetchJson<{ total: number; entries: LogEntry[] }>(`/api/logs?${params}`)
      .then((page) => {
        setEntries(page.entries)
        setTotal(page.total)
        setError(null)
      })
      .catch((err: Error) => setError(err.message))
    fetchJson<WebhookDelivery[]>('/api/logs/webhook-deliveries')
      .then(setDeliveries)
      .catch(() => undefined)
  }, [level, jobId, serial, q])

  useEffect(() => {
    load()
  }, [load])

  const retry = async (delivery: WebhookDelivery) => {
    try {
      const updated = await fetchJson<WebhookDelivery>(
        `/api/logs/webhook-deliveries/${delivery.id}/retry`,
        { method: 'POST' },
      )
      setDeliveries((prev) => prev.map((d) => (d.id === updated.id ? updated : d)))
    } catch (err) {
      setError((err as Error).message)
    }
  }

  const failedDeliveries = deliveries.filter((d) => d.status === 'failed')

  return (
    <div className="max-w-5xl">
      <h1 className="text-2xl font-bold tracking-tight">Logs</h1>

      {failedDeliveries.length > 0 && (
        <section className="mt-4" aria-label="Failed webhook deliveries">
          <h2 className="font-semibold text-amber-700 dark:text-amber-400">
            Failed webhook deliveries ({failedDeliveries.length})
          </h2>
          <ul className="mt-2 flex flex-col gap-1">
            {failedDeliveries.map((delivery) => (
              <li
                key={delivery.id}
                className="flex items-center justify-between rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm dark:border-amber-900 dark:bg-amber-950/40"
              >
                <span>
                  Job #{delivery.job_id} ·{' '}
                  <span className="font-mono">{delivery.device_serial}</span> · {delivery.attempts}{' '}
                  attempt(s) · {delivery.last_error ?? 'unknown error'}
                </span>
                <button
                  type="button"
                  className="rounded-md border border-amber-400 px-3 py-1 text-sm font-medium hover:bg-amber-100 dark:hover:bg-amber-900/40"
                  onClick={() => void retry(delivery)}
                >
                  Retry webhook
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="mt-6 flex flex-wrap items-center gap-2" aria-label="Log filters">
        <select
          aria-label="Level"
          className={inputClass}
          value={level}
          onChange={(e) => setLevel(e.target.value)}
        >
          <option value="">all levels</option>
          <option value="info">info</option>
          <option value="warning">warning</option>
          <option value="error">error</option>
        </select>
        <input
          aria-label="Job ID"
          className={inputClass + ' w-24'}
          placeholder="job #"
          value={jobId}
          onChange={(e) => setJobId(e.target.value)}
        />
        <input
          aria-label="Serial"
          className={inputClass + ' w-40 font-mono'}
          placeholder="serial"
          value={serial}
          onChange={(e) => setSerial(e.target.value)}
        />
        <input
          aria-label="Search"
          type="search"
          className={inputClass + ' w-56'}
          placeholder="search message…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <span className="text-sm text-slate-500 dark:text-slate-400">{total} entries</span>
      </div>

      {error && (
        <p role="alert" className="mt-4 rounded-md bg-rose-100 px-3 py-2 text-sm text-rose-800">
          {error}
        </p>
      )}

      <ul className="mt-4 flex flex-col gap-1">
        {entries.map((entry) => (
          <li
            key={entry.id}
            className="rounded-md border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900"
          >
            <button
              type="button"
              className="flex w-full flex-wrap items-center gap-2 px-3 py-2 text-left text-sm"
              onClick={() => setExpanded(expanded === entry.id ? null : entry.id)}
            >
              <span className="text-xs text-slate-400 tabular-nums">
                {new Date(entry.timestamp).toLocaleString()}
              </span>
              <span
                className={`rounded px-1.5 py-0.5 text-xs ${LEVEL_STYLES[entry.level] ?? LEVEL_STYLES.INFO}`}
              >
                {entry.level}
              </span>
              <span className="text-xs text-slate-400">{entry.component}</span>
              {entry.job_id !== null && (
                <span className="text-xs text-slate-400">job #{entry.job_id}</span>
              )}
              {entry.device_serial && (
                <span className="font-mono text-xs text-slate-400">{entry.device_serial}</span>
              )}
              <span className="basis-full">{entry.message}</span>
            </button>
            {expanded === entry.id && entry.context && (
              <pre className="overflow-x-auto border-t border-slate-100 px-3 py-2 font-mono text-xs text-slate-600 dark:border-slate-800 dark:text-slate-300">
                {JSON.stringify(entry.context, null, 2)}
              </pre>
            )}
          </li>
        ))}
        {entries.length === 0 && !error && (
          <li className="py-6 text-center text-sm text-slate-400">No log entries.</li>
        )}
      </ul>
    </div>
  )
}
