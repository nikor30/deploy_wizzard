import { useEffect, useState } from 'react'

interface StatsPayload {
  days: number
  totals: { jobs: number; devices: number; claimed: number; provisioned: number; failed: number }
  success_rate: number | null
  avg_day0_seconds: number | null
  avg_dayn_seconds: number | null
  failures_by_category: Record<string, number>
  jobs_over_time: { date: string; jobs: number; succeeded: number; failed: number }[]
}

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json() as Promise<T>
}

// Palette per the dataviz method (validated light+dark with the skill's checker):
// succeeded = categorical blue, failed = categorical orange.
const vizStyle = `
.viz-root {
  color-scheme: light;
  --viz-surface: #fcfcfb;
  --viz-grid: #e4e3df;
  --viz-text: #52514e;
  --viz-succeeded: #2a78d6;
  --viz-failed: #eb6834;
}
@media (prefers-color-scheme: dark) {
  .viz-root {
    color-scheme: dark;
    --viz-surface: #1a1a19;
    --viz-grid: #3a3936;
    --viz-text: #c3c2b7;
    --viz-succeeded: #3987e5;
    --viz-failed: #d95926;
  }
}
`

function formatSeconds(value: number | null): string {
  if (value === null) return '—'
  if (value < 90) return `${Math.round(value)}s`
  return `${(value / 60).toFixed(1)}min`
}

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
      <p className="text-xs tracking-wider text-slate-500 uppercase dark:text-slate-400">{label}</p>
      <p className="mt-1 text-2xl font-bold tabular-nums">{value}</p>
    </div>
  )
}

function JobsOverTimeChart({ data }: { data: StatsPayload['jobs_over_time'] }) {
  const width = 640
  const height = 200
  const pad = { top: 12, right: 8, bottom: 24, left: 28 }
  const max = Math.max(1, ...data.map((d) => Math.max(d.succeeded, d.failed)))
  const innerW = width - pad.left - pad.right
  const innerH = height - pad.top - pad.bottom
  const group = innerW / Math.max(1, data.length)
  const barW = Math.min(18, Math.max(4, group / 2 - 4))
  const y = (v: number) => pad.top + innerH - (v / max) * innerH

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label="Devices succeeded and failed per day"
      className="w-full"
      style={{ background: 'var(--viz-surface)', borderRadius: 8 }}
    >
      {[...new Set([0, Math.round(max / 2), max])].map((tick) => (
        <g key={tick}>
          <line
            x1={pad.left}
            x2={width - pad.right}
            y1={y(tick)}
            y2={y(tick)}
            stroke="var(--viz-grid)"
            strokeWidth={1}
          />
          <text
            x={pad.left - 6}
            y={y(tick) + 4}
            textAnchor="end"
            fontSize={10}
            fill="var(--viz-text)"
          >
            {tick}
          </text>
        </g>
      ))}
      {data.map((d, i) => {
        const x0 = pad.left + i * group + group / 2
        return (
          <g key={d.date}>
            <rect
              x={x0 - barW - 1}
              y={y(d.succeeded)}
              width={barW}
              height={Math.max(0, pad.top + innerH - y(d.succeeded))}
              rx={2}
              fill="var(--viz-succeeded)"
            >
              <title>{`${d.date}: ${d.succeeded} succeeded`}</title>
            </rect>
            <rect
              x={x0 + 1}
              y={y(d.failed)}
              width={barW}
              height={Math.max(0, pad.top + innerH - y(d.failed))}
              rx={2}
              fill="var(--viz-failed)"
            >
              <title>{`${d.date}: ${d.failed} failed`}</title>
            </rect>
            <text x={x0} y={height - 8} textAnchor="middle" fontSize={10} fill="var(--viz-text)">
              {d.date.slice(5)}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

function FailuresChart({ data }: { data: Record<string, number> }) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1])
  if (entries.length === 0)
    return <p className="text-sm text-slate-400">No failures in this window. 🎉</p>
  const max = Math.max(...entries.map(([, v]) => v))
  const rowH = 26
  const width = 640
  const labelW = 120
  return (
    <svg
      viewBox={`0 0 ${width} ${entries.length * rowH + 8}`}
      role="img"
      aria-label="Failures by error category"
      className="w-full"
      style={{ background: 'var(--viz-surface)', borderRadius: 8 }}
    >
      {entries.map(([category, count], i) => {
        const barLength = ((width - labelW - 60) * count) / max
        return (
          <g key={category} transform={`translate(0, ${i * rowH + 6})`}>
            <text x={labelW - 8} y={14} textAnchor="end" fontSize={11} fill="var(--viz-text)">
              {category}
            </text>
            <rect
              x={labelW}
              y={2}
              width={Math.max(2, barLength)}
              height={16}
              rx={2}
              fill="var(--viz-failed)"
            >
              <title>{`${category}: ${count}`}</title>
            </rect>
            <text
              x={labelW + Math.max(2, barLength) + 6}
              y={14}
              fontSize={11}
              fill="var(--viz-text)"
            >
              {count}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

export default function Stats() {
  const [days, setDays] = useState(30)
  const [stats, setStats] = useState<StatsPayload | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchJson<StatsPayload>(`/api/stats?days=${days}`)
      .then((payload) => {
        setStats(payload)
        setError(null)
      })
      .catch((err: Error) => setError(err.message))
  }, [days])

  return (
    <div className="viz-root max-w-4xl">
      <style>{vizStyle}</style>
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">Statistics</h1>
        <label className="text-sm">
          Time range{' '}
          <select
            className="ml-1 rounded-md border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
          >
            <option value={7}>7 days</option>
            <option value={30}>30 days</option>
            <option value={90}>90 days</option>
          </select>
        </label>
      </div>
      {error && (
        <p role="alert" className="mt-4 rounded-md bg-rose-100 px-3 py-2 text-sm text-rose-800">
          {error}
        </p>
      )}
      {stats && (
        <>
          <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <Tile label="Jobs" value={String(stats.totals.jobs)} />
            <Tile label="Claimed" value={String(stats.totals.claimed)} />
            <Tile label="Provisioned" value={String(stats.totals.provisioned)} />
            <Tile label="Failed" value={String(stats.totals.failed)} />
            <Tile
              label="Success rate"
              value={stats.success_rate === null ? '—' : `${Math.round(stats.success_rate * 100)}%`}
            />
            <Tile
              label="Avg Day-0 / Day-N"
              value={`${formatSeconds(stats.avg_day0_seconds)} / ${formatSeconds(stats.avg_dayn_seconds)}`}
            />
          </div>

          <section className="mt-8" aria-label="Jobs over time">
            <div className="flex items-center gap-4">
              <h2 className="font-semibold">Devices per day</h2>
              <span className="flex items-center gap-1 text-xs text-slate-500 dark:text-slate-400">
                <span
                  className="inline-block h-3 w-3 rounded-sm"
                  style={{ background: 'var(--viz-succeeded)' }}
                />
                succeeded
              </span>
              <span className="flex items-center gap-1 text-xs text-slate-500 dark:text-slate-400">
                <span
                  className="inline-block h-3 w-3 rounded-sm"
                  style={{ background: 'var(--viz-failed)' }}
                />
                failed
              </span>
            </div>
            <div className="mt-2">
              {stats.jobs_over_time.length === 0 ? (
                <p className="text-sm text-slate-400">No jobs in this window.</p>
              ) : (
                <JobsOverTimeChart data={stats.jobs_over_time} />
              )}
            </div>
          </section>

          <section className="mt-8" aria-label="Failures by category">
            <h2 className="font-semibold">Failures by category</h2>
            <div className="mt-2">
              <FailuresChart data={stats.failures_by_category} />
            </div>
          </section>
        </>
      )}
    </div>
  )
}
