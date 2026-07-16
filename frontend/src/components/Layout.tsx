import { NavLink, Outlet } from 'react-router-dom'

const navigation = [
  { to: '/', label: 'Wizard', end: true },
  { to: '/stats', label: 'Statistics' },
  { to: '/logs', label: 'Logs' },
]

const settingsNav = [
  { to: '/settings/credentials', label: 'Credentials' },
  { to: '/settings/mapping', label: 'Site Mapping' },
  { to: '/settings/dayn', label: 'Day-N Variables' },
]

function linkClass({ isActive }: { isActive: boolean }): string {
  return [
    'block rounded-md px-3 py-2 text-sm font-medium transition-colors',
    isActive
      ? 'bg-sky-600/10 text-sky-700 dark:bg-sky-400/10 dark:text-sky-300'
      : 'text-slate-600 hover:bg-slate-200/60 dark:text-slate-300 dark:hover:bg-slate-800',
  ].join(' ')
}

export default function Layout() {
  return (
    <div className="flex min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <aside className="flex w-60 shrink-0 flex-col border-r border-slate-200 bg-white px-4 py-6 dark:border-slate-800 dark:bg-slate-900">
        <div className="mb-8 px-3">
          <span className="text-lg font-bold tracking-tight">PnP Bridge</span>
          <p className="text-xs text-slate-500 dark:text-slate-400">NetBox ↔ Catalyst Center</p>
        </div>
        <nav className="flex flex-1 flex-col gap-1" aria-label="Main">
          {navigation.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className={linkClass}>
              {item.label}
            </NavLink>
          ))}
          <p className="mt-6 mb-1 px-3 text-xs font-semibold tracking-wider text-slate-400 uppercase dark:text-slate-500">
            Settings
          </p>
          {settingsNav.map((item) => (
            <NavLink key={item.to} to={item.to} className={linkClass}>
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main className="min-w-0 flex-1 px-8 py-8">
        <Outlet />
      </main>
    </div>
  )
}
