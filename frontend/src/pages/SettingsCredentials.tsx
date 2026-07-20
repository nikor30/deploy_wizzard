import { useEffect, useState } from 'react'
import {
  getCredentials,
  putCredentials,
  testConnection,
  type Credentials,
  type ServiceSettingsInput,
  type TestResult,
} from '../api'

type ServiceKey = 'catalyst' | 'netbox' | 'webhook'

interface FormBlock {
  base_url: string
  username: string
  secret: string
  secret_masked: string | null
  tls_verify: boolean
  enabled: boolean
}

type FormState = Record<ServiceKey, FormBlock>

function toForm(credentials: Credentials): FormState {
  const blocks = {} as FormState
  for (const key of ['catalyst', 'netbox', 'webhook'] as const) {
    const svc = credentials[key]
    blocks[key] = {
      base_url: svc.base_url ?? '',
      username: svc.username ?? '',
      secret: '',
      secret_masked: svc.secret_masked,
      tls_verify: svc.tls_verify,
      enabled: svc.enabled,
    }
  }
  return blocks
}

function toInput(block: FormBlock): ServiceSettingsInput {
  return {
    base_url: block.base_url || null,
    username: block.username || null,
    // Empty input = keep the stored secret (backend treats null as "keep").
    secret: block.secret === '' ? null : block.secret,
    tls_verify: block.tls_verify,
    enabled: block.enabled,
  }
}

const inputClass =
  'mt-1 block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm ' +
  'shadow-sm focus:border-sky-500 focus:ring-1 focus:ring-sky-500 focus:outline-none ' +
  'dark:border-slate-700 dark:bg-slate-900'

function Field({
  label,
  id,
  type = 'text',
  value,
  placeholder,
  onChange,
}: {
  label: string
  id: string
  type?: string
  value: string
  placeholder?: string
  onChange: (value: string) => void
}) {
  return (
    <label className="block" htmlFor={id}>
      <span className="text-sm font-medium text-slate-700 dark:text-slate-300">{label}</span>
      <input
        id={id}
        type={type}
        className={inputClass}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        autoComplete="off"
      />
    </label>
  )
}

function Toggle({
  label,
  id,
  checked,
  onChange,
}: {
  label: string
  id: string
  checked: boolean
  onChange: (value: boolean) => void
}) {
  return (
    <label className="flex items-center gap-2 text-sm" htmlFor={id}>
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 rounded border-slate-300 text-sky-600 focus:ring-sky-500"
      />
      {label}
    </label>
  )
}

function StatusBanner({ result }: { result: TestResult | null }) {
  if (!result) return null
  return (
    <p
      role="status"
      className={
        'rounded-md px-3 py-2 text-sm ' +
        (result.ok
          ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300'
          : 'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300')
      }
    >
      {result.detail}
    </p>
  )
}

export default function SettingsCredentials() {
  const [form, setForm] = useState<FormState | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saveState, setSaveState] = useState<TestResult | null>(null)
  const [testResults, setTestResults] = useState<Partial<Record<ServiceKey, TestResult>>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [debug, setDebug] = useState(false)

  useEffect(() => {
    getCredentials()
      .then((credentials) => setForm(toForm(credentials)))
      .catch((err: Error) => setLoadError(err.message))
    fetch('/api/settings/flags')
      .then((r) => r.json())
      .then((f: { debug: boolean }) => setDebug(f.debug))
      .catch(() => setDebug(false))
  }, [])

  const toggleDebug = async (value: boolean) => {
    setDebug(value)
    await fetch('/api/settings/flags', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ debug: value }),
    }).catch(() => setDebug(!value))
  }

  if (loadError) {
    return <StatusBanner result={{ ok: false, detail: `Cannot load settings: ${loadError}` }} />
  }
  if (!form) {
    return <p className="text-slate-500">Loading…</p>
  }

  const update = (service: ServiceKey, patch: Partial<FormBlock>) =>
    setForm({ ...form, [service]: { ...form[service], ...patch } })

  const save = async () => {
    setBusy('save')
    setSaveState(null)
    try {
      const credentials = await putCredentials({
        catalyst: toInput(form.catalyst),
        netbox: toInput(form.netbox),
        webhook: toInput(form.webhook),
      })
      setForm(toForm(credentials))
      setSaveState({ ok: true, detail: 'Settings saved.' })
    } catch (err) {
      setSaveState({ ok: false, detail: (err as Error).message })
    } finally {
      setBusy(null)
    }
  }

  const runTest = async (service: 'catalyst' | 'netbox') => {
    setBusy(service)
    setTestResults((prev) => ({ ...prev, [service]: undefined }))
    try {
      const result = await testConnection(service, toInput(form[service]))
      setTestResults((prev) => ({ ...prev, [service]: result }))
    } catch (err) {
      setTestResults((prev) => ({
        ...prev,
        [service]: { ok: false, detail: (err as Error).message },
      }))
    } finally {
      setBusy(null)
    }
  }

  const cardClass =
    'rounded-lg border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900'
  const buttonClass =
    'rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium hover:bg-slate-100 ' +
    'disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800'

  return (
    <div className="max-w-3xl">
      <h1 className="text-2xl font-bold tracking-tight">Credentials</h1>
      <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
        Stored secrets are encrypted at rest and shown masked. Leave a secret field empty to keep
        the stored value.
      </p>

      <div className="mt-6 flex flex-col gap-6">
        <section className={cardClass} aria-label="Catalyst Center">
          <h2 className="text-lg font-semibold">Cisco Catalyst Center</h2>
          <div className="mt-4 flex flex-col gap-4">
            <Field
              label="Base URL"
              id="catalyst-url"
              value={form.catalyst.base_url}
              placeholder="https://ccc.example.com"
              onChange={(v) => update('catalyst', { base_url: v })}
            />
            <Field
              label="Username"
              id="catalyst-username"
              value={form.catalyst.username}
              onChange={(v) => update('catalyst', { username: v })}
            />
            <Field
              label="Password"
              id="catalyst-password"
              type="password"
              value={form.catalyst.secret}
              placeholder={form.catalyst.secret_masked ?? ''}
              onChange={(v) => update('catalyst', { secret: v })}
            />
            <Toggle
              label="Verify TLS certificate"
              id="catalyst-tls"
              checked={form.catalyst.tls_verify}
              onChange={(v) => update('catalyst', { tls_verify: v })}
            />
            <div className="flex items-center gap-3">
              <button
                type="button"
                className={buttonClass}
                disabled={busy !== null}
                onClick={() => void runTest('catalyst')}
              >
                {busy === 'catalyst' ? 'Testing…' : 'Test connection'}
              </button>
            </div>
            <StatusBanner result={testResults.catalyst ?? null} />
          </div>
        </section>

        <section className={cardClass} aria-label="NetBox">
          <h2 className="text-lg font-semibold">NetBox</h2>
          <div className="mt-4 flex flex-col gap-4">
            <Field
              label="Base URL"
              id="netbox-url"
              value={form.netbox.base_url}
              placeholder="https://netbox.example.com"
              onChange={(v) => update('netbox', { base_url: v })}
            />
            <Field
              label="API token"
              id="netbox-token"
              type="password"
              value={form.netbox.secret}
              placeholder={form.netbox.secret_masked ?? ''}
              onChange={(v) => update('netbox', { secret: v })}
            />
            <Toggle
              label="Verify TLS certificate"
              id="netbox-tls"
              checked={form.netbox.tls_verify}
              onChange={(v) => update('netbox', { tls_verify: v })}
            />
            <div className="flex items-center gap-3">
              <button
                type="button"
                className={buttonClass}
                disabled={busy !== null}
                onClick={() => void runTest('netbox')}
              >
                {busy === 'netbox' ? 'Testing…' : 'Test connection'}
              </button>
            </div>
            <StatusBanner result={testResults.netbox ?? null} />
          </div>
        </section>

        <section className={cardClass} aria-label="ISE webhook">
          <h2 className="text-lg font-semibold">ISE Webhook</h2>
          <div className="mt-4 flex flex-col gap-4">
            <Field
              label="Target URL"
              id="webhook-url"
              value={form.webhook.base_url}
              placeholder="https://ise-helper.example.com/hook"
              onChange={(v) => update('webhook', { base_url: v })}
            />
            <Field
              label="Shared secret (HMAC-SHA256, optional)"
              id="webhook-secret"
              type="password"
              value={form.webhook.secret}
              placeholder={form.webhook.secret_masked ?? ''}
              onChange={(v) => update('webhook', { secret: v })}
            />
            <Toggle
              label="Enabled"
              id="webhook-enabled"
              checked={form.webhook.enabled}
              onChange={(v) => update('webhook', { enabled: v })}
            />
          </div>
        </section>

        <section className={cardClass} aria-label="Debug">
          <h2 className="text-lg font-semibold">Debug</h2>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Show the source of every wizard variable (netbox / mapped / manual) in the Day-0 and
            Day-N steps, so you can check what the tool prefilled and what is still open.
          </p>
          <div className="mt-3">
            <Toggle
              label="Show variable sources (debug)"
              id="debug-flag"
              checked={debug}
              onChange={(v) => void toggleDebug(v)}
            />
          </div>
        </section>

        <div className="flex items-center gap-4">
          <button
            type="button"
            className="rounded-md bg-sky-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-sky-500 disabled:opacity-50"
            disabled={busy !== null}
            onClick={() => void save()}
          >
            {busy === 'save' ? 'Saving…' : 'Save settings'}
          </button>
          <StatusBanner result={saveState} />
        </div>
      </div>
    </div>
  )
}
