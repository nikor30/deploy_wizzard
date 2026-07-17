import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

interface PnpDevice {
  ccc_device_id: string
  serial: string
  pid: string | null
  state: string | null
  ip_address: string | null
  last_contact: string | null
}

interface VlanOption {
  id: number
  vid: number
  name: string | null
}

interface JobDevice {
  id: number
  serial: string
  pid: string | null
  ccc_device_id: string
  match_status: 'matched' | 'unmatched' | 'unmapped_site' | null
  netbox_name: string | null
  netbox_site_name: string | null
  ccc_site_name: string | null
  mgmt_ip: string | null
  mgmt_vlan: number | null
  vlan_options: VlanOption[]
  state: string
  error: string | null
  dayn_variables: Record<string, { value: string | null; source: 'mapped' | 'manual' }> | null
}

interface Job {
  id: number
  status: string
  current_step: number
  created_at: string
  device_count: number
  dayn_template_id?: string | null
  devices: JobDevice[]
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

const STEPS = ['Select devices', 'Match with NetBox', 'Day-0 claim', 'Day-N provision', 'Finalize']

function Stepper({ active }: { active: number }) {
  return (
    <ol className="flex flex-wrap gap-2" aria-label="Wizard steps">
      {STEPS.map((label, index) => {
        const number = index + 1
        const state = number === active ? 'active' : number < active ? 'done' : 'todo'
        return (
          <li
            key={label}
            className={
              'flex items-center gap-2 rounded-full px-3 py-1 text-sm ' +
              (state === 'active'
                ? 'bg-sky-600 text-white'
                : state === 'done'
                  ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300'
                  : 'bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-500')
            }
          >
            <span className="font-semibold">{number}</span> {label}
          </li>
        )
      })}
    </ol>
  )
}

function ErrorBanner({ message }: { message: string | null }) {
  if (!message) return null
  return (
    <p
      role="alert"
      className="mt-4 rounded-md bg-rose-100 px-3 py-2 text-sm text-rose-800 dark:bg-rose-900/40 dark:text-rose-300"
    >
      {message}
    </p>
  )
}

const buttonPrimary =
  'rounded-md bg-sky-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-sky-500 disabled:opacity-50'
const buttonSecondary =
  'rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium hover:bg-slate-100 ' +
  'disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800'

function StartView({ onNew, onResume }: { onNew: () => void; onResume: (job: Job) => void }) {
  const [jobs, setJobs] = useState<Job[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchJson<Job[]>('/api/wizard/jobs')
      .then(setJobs)
      .catch((err: Error) => setError(err.message))
  }, [])

  return (
    <div className="mt-8">
      <button type="button" className={buttonPrimary} onClick={onNew}>
        Start new onboarding job
      </button>
      <ErrorBanner message={error} />
      {jobs && jobs.length > 0 && (
        <section className="mt-8" aria-label="Previous jobs">
          <h2 className="font-semibold">Resume a job</h2>
          <ul className="mt-2 flex flex-col gap-1">
            {jobs.map((job) => (
              <li
                key={job.id}
                className="flex items-center justify-between rounded-md border border-slate-200 bg-white px-3 py-2 text-sm dark:border-slate-800 dark:bg-slate-900"
              >
                <span>
                  Job #{job.id} · {job.device_count} device(s) · step {job.current_step} ·{' '}
                  {new Date(job.created_at).toLocaleString()}
                </span>
                <button type="button" className={buttonSecondary} onClick={() => onResume(job)}>
                  Resume
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

function SelectView({ onJobCreated }: { onJobCreated: (job: Job) => void }) {
  const [devices, setDevices] = useState<PnpDevice[] | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [search, setSearch] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(() => {
    fetchJson<PnpDevice[]>('/api/wizard/pnp-devices')
      .then((list) => {
        setDevices(list)
        setError(null)
      })
      .catch((err: Error) => setError(err.message))
  }, [])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, 60_000)
    return () => clearInterval(interval)
  }, [refresh])

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const filtered = (devices ?? []).filter(
    (d) =>
      d.serial.toLowerCase().includes(search.toLowerCase()) ||
      (d.pid ?? '').toLowerCase().includes(search.toLowerCase()),
  )

  const createJob = async () => {
    setBusy(true)
    try {
      const chosen = (devices ?? []).filter((d) => selected.has(d.ccc_device_id))
      const job = await fetchJson<Job>('/api/wizard/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          devices: chosen.map((d) => ({
            serial: d.serial,
            pid: d.pid,
            ccc_device_id: d.ccc_device_id,
          })),
        }),
      })
      onJobCreated(job)
    } catch (err) {
      setError((err as Error).message)
      setBusy(false)
    }
  }

  return (
    <div className="mt-8">
      <div className="flex items-center gap-3">
        <input
          type="search"
          placeholder="Filter by serial or PID…"
          className="block w-72 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm focus:border-sky-500 focus:outline-none dark:border-slate-700 dark:bg-slate-900"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <button type="button" className={buttonSecondary} onClick={refresh}>
          Refresh
        </button>
        <span className="text-sm text-slate-500 dark:text-slate-400">
          auto-refreshes every 60 s
        </span>
      </div>
      <ErrorBanner message={error} />
      {devices === null && !error && <p className="mt-6 text-sm text-slate-400">Loading…</p>}
      {devices !== null && (
        <div className="mt-4 overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-800">
          <table className="w-full bg-white text-left text-sm dark:bg-slate-900">
            <thead className="border-b border-slate-200 text-xs text-slate-500 uppercase dark:border-slate-800 dark:text-slate-400">
              <tr>
                <th className="px-3 py-2"></th>
                <th className="px-3 py-2">Serial</th>
                <th className="px-3 py-2">PID</th>
                <th className="px-3 py-2">Source IP</th>
                <th className="px-3 py-2">State</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((device) => (
                <tr
                  key={device.ccc_device_id}
                  className="border-b border-slate-100 last:border-0 dark:border-slate-800"
                >
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      aria-label={`Select ${device.serial}`}
                      checked={selected.has(device.ccc_device_id)}
                      onChange={() => toggle(device.ccc_device_id)}
                    />
                  </td>
                  <td className="px-3 py-2 font-mono">{device.serial}</td>
                  <td className="px-3 py-2">{device.pid ?? '—'}</td>
                  <td className="px-3 py-2">{device.ip_address ?? '—'}</td>
                  <td className="px-3 py-2">{device.state ?? '—'}</td>
                </tr>
              ))}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-3 py-6 text-center text-slate-400">
                    No unclaimed devices found.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
      <div className="mt-4">
        <button
          type="button"
          className={buttonPrimary}
          disabled={selected.size === 0 || busy}
          onClick={() => void createJob()}
        >
          {busy ? 'Creating job…' : `Continue with ${selected.size} device(s)`}
        </button>
      </div>
    </div>
  )
}

function matchBadge(status: JobDevice['match_status']) {
  if (status === 'matched')
    return (
      <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-xs text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300">
        matched
      </span>
    )
  if (status === 'unmatched')
    return (
      <span className="rounded bg-rose-100 px-1.5 py-0.5 text-xs text-rose-800 dark:bg-rose-900/40 dark:text-rose-300">
        no NetBox match
      </span>
    )
  if (status === 'unmapped_site')
    return (
      <span className="rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
        site not mapped
      </span>
    )
  return <span className="text-xs text-slate-400">pending…</span>
}

function MatchView({ job: initialJob, onContinue }: { job: Job; onContinue: (job: Job) => void }) {
  const [job, setJob] = useState<Job>(initialJob)
  const [error, setError] = useState<string | null>(null)
  const [matching, setMatching] = useState(false)

  useEffect(() => {
    setMatching(true)
    fetchJson<Job>(`/api/wizard/jobs/${initialJob.id}/match`, { method: 'POST' })
      .then((matched) => {
        setJob(matched)
        setError(null)
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setMatching(false))
  }, [initialJob.id])

  const pickVlan = async (device: JobDevice, vid: number | null) => {
    try {
      const updated = await fetchJson<JobDevice>(
        `/api/wizard/jobs/${job.id}/devices/${device.id}`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mgmt_vlan: vid }),
        },
      )
      setJob((prev) => ({
        ...prev,
        devices: prev.devices.map((d) => (d.id === updated.id ? updated : d)),
      }))
    } catch (err) {
      setError((err as Error).message)
    }
  }

  const claimable = job.devices.filter((d) => d.match_status === 'matched')

  return (
    <div className="mt-8">
      <p className="text-sm text-slate-500 dark:text-slate-400">
        Job #{job.id} — matching CCC serials against NetBox devices with status <code>planned</code>
        . {matching && 'Matching…'}
      </p>
      <ErrorBanner message={error} />
      <div className="mt-4 flex flex-col gap-3">
        {job.devices.map((device) => (
          <section
            key={device.id}
            aria-label={`Match ${device.serial}`}
            className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-mono font-semibold">{device.serial}</span>
              {matchBadge(device.match_status)}
            </div>
            <div className="mt-3 grid gap-4 text-sm sm:grid-cols-2">
              <div>
                <h3 className="text-xs text-slate-400 uppercase">Catalyst Center</h3>
                <p className="mt-1">PID: {device.pid ?? '—'}</p>
                <p>Target site: {device.ccc_site_name ?? '—'}</p>
              </div>
              <div>
                <h3 className="text-xs text-slate-400 uppercase">NetBox</h3>
                {device.match_status === 'unmatched' ? (
                  <p className="mt-1 text-slate-400">
                    No planned device with this serial — excluded from claiming.
                  </p>
                ) : (
                  <>
                    <p className="mt-1">Name: {device.netbox_name ?? '—'}</p>
                    <p>Site: {device.netbox_site_name ?? '—'}</p>
                    <p>Mgmt IP: {device.mgmt_ip ?? '—'}</p>
                    {device.match_status === 'unmapped_site' && (
                      <p className="mt-1 text-amber-700 dark:text-amber-300">
                        Map this site first:{' '}
                        <Link to="/settings/mapping" className="underline">
                          Settings → Site Mapping
                        </Link>
                      </p>
                    )}
                    {device.match_status === 'matched' && (
                      <label className="mt-2 block">
                        <span className="text-xs text-slate-400 uppercase">Mgmt VLAN</span>
                        <select
                          className="mt-1 block w-48 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
                          value={device.mgmt_vlan ?? ''}
                          onChange={(e) =>
                            void pickVlan(
                              device,
                              e.target.value === '' ? null : Number(e.target.value),
                            )
                          }
                        >
                          <option value="">— select VLAN —</option>
                          {device.vlan_options.map((vlan) => (
                            <option key={vlan.id} value={vlan.vid}>
                              {vlan.vid} {vlan.name ? `(${vlan.name})` : ''}
                            </option>
                          ))}
                        </select>
                      </label>
                    )}
                  </>
                )}
              </div>
            </div>
          </section>
        ))}
      </div>
      <div className="mt-6 flex items-center gap-3">
        <button
          type="button"
          className={buttonPrimary}
          disabled={claimable.length === 0 || matching}
          onClick={() => onContinue(job)}
        >
          Continue to Day-0 claim ({claimable.length} device(s))
        </button>
        {claimable.length === 0 && !matching && (
          <span className="text-sm text-slate-400">No claimable devices in this job.</span>
        )}
      </div>
    </div>
  )
}

const DEVICE_STATE_STYLES: Record<string, string> = {
  queued: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300',
  claiming: 'bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-300',
  provisioning: 'bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-300',
  success: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300',
  failed: 'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300',
}

function stateBadge(state: string) {
  const style = DEVICE_STATE_STYLES[state] ?? 'bg-slate-100 text-slate-500 dark:bg-slate-800'
  return <span className={`rounded px-1.5 py-0.5 text-xs ${style}`}>{state}</span>
}

interface Day0Template {
  id: string
  name: string
  project: string | null
}

const isTerminal = (status: string) => !status.endsWith('_running')

function useJobWatch(
  jobId: number,
  running: boolean,
  setJob: (job: Job) => void,
  setRunning: (running: boolean) => void,
) {
  // Live progress: SSE when the browser supports it, 2 s polling otherwise.
  useEffect(() => {
    if (!running) return
    if (typeof EventSource !== 'undefined') {
      const source = new EventSource(`/api/wizard/jobs/${jobId}/events`)
      source.onmessage = (event) => {
        const snapshot = JSON.parse(event.data as string) as Job
        setJob(snapshot)
        if (isTerminal(snapshot.status)) {
          setRunning(false)
          source.close()
        }
      }
      source.onerror = () => {
        source.close()
        setRunning(false)
      }
      return () => source.close()
    }
    const interval = setInterval(() => {
      fetchJson<Job>(`/api/wizard/jobs/${jobId}`)
        .then((snapshot) => {
          setJob(snapshot)
          if (isTerminal(snapshot.status)) setRunning(false)
        })
        .catch(() => undefined)
    }, 2000)
    return () => clearInterval(interval)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running, jobId])
}

function Day0View({ job: initialJob, onContinue }: { job: Job; onContinue: (job: Job) => void }) {
  const [job, setJob] = useState<Job>(initialJob)
  const [templates, setTemplates] = useState<Day0Template[] | null>(null)
  const [configId, setConfigId] = useState('')
  const [imageId, setImageId] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(initialJob.status === 'day0_running')

  useEffect(() => {
    fetchJson<Day0Template[]>('/api/wizard/day0/templates')
      .then(setTemplates)
      .catch((err: Error) => setError(err.message))
  }, [])

  useJobWatch(job.id, running, setJob, setRunning)

  const start = async () => {
    setError(null)
    try {
      const started = await fetchJson<Job>(`/api/wizard/jobs/${job.id}/claim`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config_id: configId, image_id: imageId || null }),
      })
      setJob(started)
      setRunning(true)
    } catch (err) {
      setError((err as Error).message)
    }
  }

  const claimable = job.devices.filter((d) => d.match_status === 'matched')
  const finished = isTerminal(job.status) && job.status.startsWith('day0_')
  const succeeded = claimable.filter((d) => d.state === 'success').length
  const failed = claimable.filter((d) => d.state === 'failed').length

  return (
    <div className="mt-8">
      <ErrorBanner message={error} />
      {!running && !finished && (
        <section
          aria-label="Day-0 configuration"
          className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
        >
          <h2 className="font-semibold">Day-0 onboarding configuration</h2>
          <div className="mt-3 flex flex-wrap gap-4">
            <label className="block">
              <span className="text-xs text-slate-400 uppercase">Onboarding template</span>
              <select
                className="mt-1 block w-72 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
                value={configId}
                onChange={(e) => setConfigId(e.target.value)}
              >
                <option value="">— select template —</option>
                {(templates ?? []).map((template) => (
                  <option key={template.id} value={template.id}>
                    {template.project ? `${template.project} / ` : ''}
                    {template.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="text-xs text-slate-400 uppercase">Image ID (optional)</span>
              <input
                type="text"
                className="mt-1 block w-72 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
                value={imageId}
                placeholder="leave empty to skip image install"
                onChange={(e) => setImageId(e.target.value)}
              />
            </label>
          </div>
        </section>
      )}

      <div className="mt-4 flex flex-col gap-3">
        {claimable.map((device) => (
          <section
            key={device.id}
            aria-label={`Day-0 ${device.serial}`}
            className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-mono font-semibold">{device.serial}</span>
              {stateBadge(device.state)}
            </div>
            <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
              {device.netbox_name} → {device.ccc_site_name} · IP {device.mgmt_ip ?? '—'} · VLAN{' '}
              {device.mgmt_vlan ?? '—'}
            </p>
            {device.error && (
              <p className="mt-2 text-sm text-rose-700 dark:text-rose-300">{device.error}</p>
            )}
          </section>
        ))}
      </div>

      <div className="mt-6 flex items-center gap-3">
        {!running && !finished && (
          <button
            type="button"
            className={buttonPrimary}
            disabled={!configId}
            onClick={() => void start()}
          >
            Start Day-0 claim ({claimable.length} device(s))
          </button>
        )}
        {running && <span className="text-sm text-sky-600 dark:text-sky-400">Claiming…</span>}
        {finished && (
          <>
            <span
              role="status"
              className="rounded-md bg-slate-100 px-3 py-2 text-sm dark:bg-slate-800"
            >
              Day-0 finished: {succeeded} succeeded, {failed} failed.
            </span>
            <button
              type="button"
              className={buttonPrimary}
              disabled={succeeded === 0}
              onClick={() => onContinue(job)}
            >
              Continue to Day-N ({succeeded} device(s))
            </button>
          </>
        )}
      </div>
    </div>
  )
}

const JOB_DONE_STATUSES = ['completed', 'partial_success', 'dayn_failed']

function DayNView({ job: initialJob }: { job: Job }) {
  const [job, setJob] = useState<Job>(initialJob)
  const [templates, setTemplates] = useState<Day0Template[] | null>(null)
  const [templateId, setTemplateId] = useState(initialJob.dayn_template_id ?? '')
  const [prepared, setPrepared] = useState(
    initialJob.devices.some((d) => d.dayn_variables !== null),
  )
  const [manual, setManual] = useState<Record<number, Record<string, string>>>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [running, setRunning] = useState(initialJob.status === 'dayn_running')

  useEffect(() => {
    fetchJson<Day0Template[]>('/api/wizard/day0/templates')
      .then(setTemplates)
      .catch((err: Error) => setError(err.message))
  }, [])

  useJobWatch(job.id, running, setJob, setRunning)

  const eligible = job.devices.filter((d) =>
    [
      'success',
      'dayn_failed',
      'activate_failed',
      'dayn_queued',
      'dayn_deploying',
      'completed',
    ].includes(d.state),
  )
  const done = JOB_DONE_STATUSES.includes(job.status)

  const prepare = async () => {
    setBusy(true)
    setError(null)
    try {
      const updated = await fetchJson<Job>(`/api/wizard/jobs/${job.id}/dayn/prepare`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ template_id: templateId }),
      })
      setJob(updated)
      setPrepared(true)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const deploy = async () => {
    setBusy(true)
    setError(null)
    try {
      const started = await fetchJson<Job>(`/api/wizard/jobs/${job.id}/dayn/deploy`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ template_id: templateId, manual }),
      })
      setJob(started)
      setRunning(true)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const setManualValue = (deviceId: number, variable: string, value: string) =>
    setManual((prev) => ({
      ...prev,
      [deviceId]: { ...(prev[deviceId] ?? {}), [variable]: value },
    }))

  const manualComplete = eligible.every((device) =>
    Object.entries(device.dayn_variables ?? {}).every(
      ([variable, info]) =>
        info.source !== 'manual' || (manual[device.id]?.[variable] ?? '').trim() !== '',
    ),
  )

  if (done) {
    const completed = job.devices.filter((d) => d.state === 'completed')
    const activateFailed = job.devices.filter((d) => d.state === 'activate_failed')
    const daynFailed = job.devices.filter((d) => d.state === 'dayn_failed')
    const banner =
      job.status === 'completed'
        ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300'
        : job.status === 'partial_success'
          ? 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300'
          : 'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300'
    return (
      <div className="mt-8">
        <p role="status" className={`rounded-md px-4 py-3 text-sm font-medium ${banner}`}>
          Job #{job.id} {job.status.replace('_', ' ')}: {completed.length} device(s) active in
          NetBox, {activateFailed.length} activation failure(s), {daynFailed.length} Day-N
          failure(s).
        </p>
        <div className="mt-4 flex flex-col gap-3">
          {job.devices.map((device) => (
            <section
              key={device.id}
              aria-label={`Summary ${device.serial}`}
              className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-mono font-semibold">{device.serial}</span>
                {stateBadge(device.state)}
              </div>
              <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
                {device.netbox_name ?? '—'} · {device.ccc_site_name ?? '—'}
              </p>
              {device.error && (
                <p className="mt-2 text-sm text-rose-700 dark:text-rose-300">{device.error}</p>
              )}
            </section>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="mt-8">
      <ErrorBanner message={error} />
      {!running && (
        <section
          aria-label="Day-N configuration"
          className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
        >
          <h2 className="font-semibold">Day-N template</h2>
          <div className="mt-3 flex flex-wrap items-end gap-4">
            <label className="block">
              <span className="text-xs text-slate-400 uppercase">Template</span>
              <select
                className="mt-1 block w-72 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
                value={templateId}
                onChange={(e) => setTemplateId(e.target.value)}
              >
                <option value="">— select template —</option>
                {(templates ?? []).map((template) => (
                  <option key={template.id} value={template.id}>
                    {template.project ? `${template.project} / ` : ''}
                    {template.name}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className={buttonSecondary}
              disabled={!templateId || busy}
              onClick={() => void prepare()}
            >
              {busy ? 'Working…' : 'Resolve variables'}
            </button>
          </div>
        </section>
      )}

      {prepared && (
        <div className="mt-4 flex flex-col gap-3">
          {eligible.map((device) => (
            <section
              key={device.id}
              aria-label={`Day-N ${device.serial}`}
              className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-mono font-semibold">{device.serial}</span>
                {stateBadge(device.state)}
              </div>
              {device.error && (
                <p className="mt-2 text-sm text-rose-700 dark:text-rose-300">{device.error}</p>
              )}
              <div className="mt-3 grid gap-2 sm:grid-cols-2">
                {Object.entries(device.dayn_variables ?? {}).map(([variable, info]) =>
                  info.source === 'manual' ? (
                    <label key={variable} className="block">
                      <span className="text-xs text-amber-600 uppercase dark:text-amber-400">
                        {variable} (manual)
                      </span>
                      <input
                        type="text"
                        required
                        className="mt-1 block w-full rounded-md border border-amber-300 bg-white px-2 py-1.5 text-sm dark:border-amber-700 dark:bg-slate-900"
                        value={manual[device.id]?.[variable] ?? ''}
                        onChange={(e) => setManualValue(device.id, variable, e.target.value)}
                      />
                    </label>
                  ) : (
                    <div key={variable}>
                      <span className="text-xs text-slate-400 uppercase">{variable}</span>
                      <p className="mt-1 rounded-md bg-slate-50 px-2 py-1.5 font-mono text-sm dark:bg-slate-800">
                        {String(info.value)}
                      </p>
                    </div>
                  ),
                )}
              </div>
            </section>
          ))}
        </div>
      )}

      <div className="mt-6 flex items-center gap-3">
        {!running && prepared && (
          <button
            type="button"
            className={buttonPrimary}
            disabled={!manualComplete || busy}
            onClick={() => void deploy()}
          >
            Deploy Day-N ({eligible.length} device(s))
          </button>
        )}
        {!running && prepared && !manualComplete && (
          <span className="text-sm text-amber-600 dark:text-amber-400">
            Fill in all manual variables first.
          </span>
        )}
        {running && <span className="text-sm text-sky-600 dark:text-sky-400">Deploying…</span>}
      </div>
    </div>
  )
}

const STEP_FOR_VIEW: Record<string, number> = { start: 1, select: 1, match: 2, day0: 3, dayn: 4 }

export default function Wizard() {
  const [view, setView] = useState<'start' | 'select' | 'match' | 'day0' | 'dayn'>('start')
  const [job, setJob] = useState<Job | null>(null)

  const openJob = (selected: Job) => {
    setJob(selected)
    // Resume where the job left off.
    if (selected.status.startsWith('dayn_') || JOB_DONE_STATUSES.includes(selected.status)) {
      setView('dayn')
    } else if (selected.status.startsWith('day0_')) {
      setView('day0')
    } else {
      setView('match')
    }
  }

  const toDay0 = (current: Job) => {
    setJob(current)
    setView('day0')
  }

  const toDayN = (current: Job) => {
    setJob(current)
    setView('dayn')
  }

  const activeStep =
    view === 'dayn' && job && JOB_DONE_STATUSES.includes(job.status) ? 5 : STEP_FOR_VIEW[view]

  return (
    <div className="max-w-5xl">
      <h1 className="text-2xl font-bold tracking-tight">Onboarding Wizard</h1>
      <div className="mt-4">
        <Stepper active={activeStep} />
      </div>
      {view === 'start' && <StartView onNew={() => setView('select')} onResume={openJob} />}
      {view === 'select' && <SelectView onJobCreated={openJob} />}
      {view === 'match' && job && <MatchView job={job} onContinue={toDay0} />}
      {view === 'day0' && job && <Day0View job={job} onContinue={toDayN} />}
      {view === 'dayn' && job && <DayNView job={job} />}
    </div>
  )
}
