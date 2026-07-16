import { useEffect, useState } from 'react'

interface Health {
  status: string
  version: string
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetch('/api/health')
      .then((res) => {
        if (!res.ok) throw new Error(`API returned ${res.status}`)
        return res.json() as Promise<Health>
      })
      .then(setHealth)
      .catch((err: Error) => setError(err.message))
  }, [])

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-4 bg-slate-50 text-slate-900 dark:bg-slate-900 dark:text-slate-100">
      <h1 className="text-3xl font-bold">PnP Bridge</h1>
      <p className="text-slate-500 dark:text-slate-400">
        NetBox ↔ Cisco Catalyst Center onboarding
      </p>
      {health && (
        <p data-testid="health" className="rounded bg-emerald-100 px-3 py-1 text-emerald-800">
          Backend {health.status} · v{health.version}
        </p>
      )}
      {error && (
        <p data-testid="health-error" className="rounded bg-rose-100 px-3 py-1 text-rose-800">
          Backend unreachable: {error}
        </p>
      )}
    </main>
  )
}
