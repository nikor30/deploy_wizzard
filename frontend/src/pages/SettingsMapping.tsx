import { useEffect, useRef, useState } from 'react'

interface NetBoxSite {
  id: number
  name: string
  slug: string | null
}

interface CccSite {
  id: string
  name_hierarchy: string
}

interface SiteMapping {
  netbox_site_id: number
  netbox_site_name: string
  ccc_site_id: string
  ccc_site_name: string
}

interface SiteSuggestion extends SiteMapping {
  confidence: number
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

const listButtonClass = (selected: boolean, mapped: boolean) =>
  [
    'block w-full truncate rounded-md px-3 py-2 text-left text-sm transition-colors',
    selected
      ? 'bg-sky-600 text-white'
      : mapped
        ? 'text-slate-400 hover:bg-slate-100 dark:text-slate-500 dark:hover:bg-slate-800'
        : 'text-slate-700 hover:bg-slate-100 dark:text-slate-200 dark:hover:bg-slate-800',
  ].join(' ')

export default function SettingsMapping() {
  const [netboxSites, setNetboxSites] = useState<NetBoxSite[] | null>(null)
  const [cccSites, setCccSites] = useState<CccSite[] | null>(null)
  const [mappings, setMappings] = useState<SiteMapping[]>([])
  const [netboxSearch, setNetboxSearch] = useState('')
  const [cccSearch, setCccSearch] = useState('')
  const [selectedNetbox, setSelectedNetbox] = useState<NetBoxSite | null>(null)
  const [banner, setBanner] = useState<Banner | null>(null)
  const [sourceError, setSourceError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    fetchJson<{ mappings: SiteMapping[] }>('/api/mappings/sites')
      .then((body) => setMappings(body.mappings))
      .catch((err: Error) => setBanner({ ok: false, detail: err.message }))
    fetchJson<NetBoxSite[]>('/api/mappings/sources/netbox')
      .then(setNetboxSites)
      .catch((err: Error) => setSourceError(err.message))
    fetchJson<CccSite[]>('/api/mappings/sources/ccc')
      .then(setCccSites)
      .catch((err: Error) => setSourceError(err.message))
  }, [])

  const [suggested, setSuggested] = useState<Record<number, number>>({})
  const [suggesting, setSuggesting] = useState(false)

  const mappedNetboxIds = new Set(mappings.map((m) => m.netbox_site_id))

  const suggest = async () => {
    setSuggesting(true)
    setBanner(null)
    try {
      const suggestions = await fetchJson<SiteSuggestion[]>('/api/mappings/sites/suggest')
      if (suggestions.length === 0) {
        setBanner({
          ok: true,
          detail: 'No confident matches for the remaining unmapped sites — pair them manually.',
        })
        return
      }
      setMappings((prev) => [
        ...prev,
        ...suggestions
          .filter((s) => !prev.some((m) => m.netbox_site_id === s.netbox_site_id))
          .map((s) => ({
            netbox_site_id: s.netbox_site_id,
            netbox_site_name: s.netbox_site_name,
            ccc_site_id: s.ccc_site_id,
            ccc_site_name: s.ccc_site_name,
          })),
      ])
      setSuggested((prev) => ({
        ...prev,
        ...Object.fromEntries(suggestions.map((s) => [s.netbox_site_id, s.confidence])),
      }))
      setBanner({
        ok: true,
        detail: `Suggested ${suggestions.length} mapping(s) — review, correct, then save.`,
      })
    } catch (err) {
      setBanner({ ok: false, detail: (err as Error).message })
    } finally {
      setSuggesting(false)
    }
  }

  const addMapping = (ccc: CccSite) => {
    if (!selectedNetbox) return
    setMappings((prev) => [
      ...prev.filter((m) => m.netbox_site_id !== selectedNetbox.id),
      {
        netbox_site_id: selectedNetbox.id,
        netbox_site_name: selectedNetbox.name,
        ccc_site_id: ccc.id,
        ccc_site_name: ccc.name_hierarchy,
      },
    ])
    setSelectedNetbox(null)
  }

  const removeMapping = (netboxSiteId: number) =>
    setMappings((prev) => prev.filter((m) => m.netbox_site_id !== netboxSiteId))

  const save = async () => {
    setBusy(true)
    setBanner(null)
    try {
      const body = await fetchJson<{ mappings: SiteMapping[] }>('/api/mappings/sites', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mappings }),
      })
      setMappings(body.mappings)
      setBanner({ ok: true, detail: 'Mappings saved.' })
    } catch (err) {
      setBanner({ ok: false, detail: (err as Error).message })
    } finally {
      setBusy(false)
    }
  }

  const exportJson = () => {
    const blob = new Blob([JSON.stringify({ mappings }, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'site-mappings.json'
    a.click()
    URL.revokeObjectURL(url)
  }

  const importJson = async (file: File) => {
    try {
      const parsed = JSON.parse(await file.text()) as { mappings?: SiteMapping[] }
      if (!Array.isArray(parsed.mappings)) throw new Error('missing "mappings" array')
      setMappings(parsed.mappings)
      setBanner({
        ok: true,
        detail: `Imported ${parsed.mappings.length} mappings — review and save.`,
      })
    } catch (err) {
      setBanner({ ok: false, detail: `Import failed: ${(err as Error).message}` })
    }
  }

  const filteredNetbox = (netboxSites ?? []).filter((site) =>
    site.name.toLowerCase().includes(netboxSearch.toLowerCase()),
  )
  const filteredCcc = (cccSites ?? []).filter((site) =>
    site.name_hierarchy.toLowerCase().includes(cccSearch.toLowerCase()),
  )

  const searchClass =
    'mb-2 block w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm ' +
    'focus:border-sky-500 focus:outline-none dark:border-slate-700 dark:bg-slate-900'
  const columnClass =
    'flex-1 rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900'
  const actionButton =
    'rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium hover:bg-slate-100 ' +
    'disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800'

  return (
    <div className="max-w-5xl">
      <h1 className="text-2xl font-bold tracking-tight">Site Mapping</h1>
      <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
        Pick a NetBox site on the left, then its Catalyst Center target on the right. Unmapped
        NetBox sites block claiming in the wizard.
      </p>

      {sourceError && (
        <p
          role="alert"
          className="mt-4 rounded-md bg-amber-100 px-3 py-2 text-sm text-amber-800 dark:bg-amber-900/40 dark:text-amber-300"
        >
          {sourceError}
        </p>
      )}

      <div className="mt-6 flex gap-6">
        <section className={columnClass} aria-label="NetBox sites">
          <h2 className="mb-3 font-semibold">NetBox sites</h2>
          <input
            type="search"
            placeholder="Search NetBox sites…"
            className={searchClass}
            value={netboxSearch}
            onChange={(e) => setNetboxSearch(e.target.value)}
          />
          <ul className="flex max-h-96 flex-col gap-0.5 overflow-y-auto">
            {netboxSites === null && !sourceError && (
              <li className="text-sm text-slate-400">Loading…</li>
            )}
            {filteredNetbox.map((site) => (
              <li key={site.id}>
                <button
                  type="button"
                  className={listButtonClass(
                    selectedNetbox?.id === site.id,
                    mappedNetboxIds.has(site.id),
                  )}
                  onClick={() => setSelectedNetbox(site)}
                >
                  {site.name}
                  {!mappedNetboxIds.has(site.id) && (
                    <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
                      unmapped
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </section>

        <section className={columnClass} aria-label="Catalyst Center sites">
          <h2 className="mb-3 font-semibold">Catalyst Center hierarchy</h2>
          <input
            type="search"
            placeholder="Search CCC sites…"
            className={searchClass}
            value={cccSearch}
            onChange={(e) => setCccSearch(e.target.value)}
          />
          {selectedNetbox ? (
            <p className="mb-2 text-sm text-sky-600 dark:text-sky-400">
              Choose the CCC site for <strong>{selectedNetbox.name}</strong>:
            </p>
          ) : (
            <p className="mb-2 text-sm text-slate-400">Select a NetBox site first.</p>
          )}
          <ul className="flex max-h-96 flex-col gap-0.5 overflow-y-auto">
            {cccSites === null && !sourceError && (
              <li className="text-sm text-slate-400">Loading…</li>
            )}
            {filteredCcc.map((site) => (
              <li key={site.id}>
                <button
                  type="button"
                  disabled={!selectedNetbox}
                  className={listButtonClass(false, false) + ' disabled:opacity-40'}
                  onClick={() => addMapping(site)}
                >
                  {site.name_hierarchy}
                </button>
              </li>
            ))}
          </ul>
        </section>
      </div>

      <div className="mt-4">
        <button
          type="button"
          className={actionButton}
          disabled={suggesting || netboxSites === null || cccSites === null}
          onClick={() => void suggest()}
        >
          {suggesting ? 'Matching…' : 'Suggest mappings'}
        </button>
        <span className="ml-3 text-sm text-slate-500 dark:text-slate-400">
          Pre-matches unmapped NetBox sites against the CCC hierarchy — review before saving.
        </span>
      </div>

      <section className="mt-6" aria-label="Current mappings">
        <h2 className="font-semibold">Mappings ({mappings.length})</h2>
        <ul className="mt-2 flex flex-col gap-1">
          {mappings.map((m) => (
            <li
              key={m.netbox_site_id}
              className="flex items-center justify-between rounded-md border border-slate-200 bg-white px-3 py-2 text-sm dark:border-slate-800 dark:bg-slate-900"
            >
              <span className="truncate">
                <strong>{m.netbox_site_name}</strong>
                <span className="mx-2 text-slate-400">→</span>
                {m.ccc_site_name}
                {suggested[m.netbox_site_id] !== undefined && (
                  <span
                    className="ml-2 rounded bg-sky-100 px-1.5 py-0.5 text-xs whitespace-nowrap text-sky-800 dark:bg-sky-900/40 dark:text-sky-300"
                    title="Suggested automatically — review before saving"
                  >
                    suggested · {Math.round(suggested[m.netbox_site_id] * 100)}%
                  </span>
                )}
              </span>
              <button
                type="button"
                className="ml-3 text-rose-600 hover:underline dark:text-rose-400"
                onClick={() => removeMapping(m.netbox_site_id)}
              >
                Remove
              </button>
            </li>
          ))}
          {mappings.length === 0 && <li className="text-sm text-slate-400">No mappings yet.</li>}
        </ul>
      </section>

      <div className="mt-6 flex items-center gap-3">
        <button
          type="button"
          className="rounded-md bg-sky-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-sky-500 disabled:opacity-50"
          disabled={busy}
          onClick={() => void save()}
        >
          {busy ? 'Saving…' : 'Save mappings'}
        </button>
        <button type="button" className={actionButton} onClick={exportJson}>
          Export JSON
        </button>
        <button type="button" className={actionButton} onClick={() => fileInput.current?.click()}>
          Import JSON
        </button>
        <input
          ref={fileInput}
          type="file"
          accept="application/json"
          className="hidden"
          data-testid="import-input"
          onChange={(e) => {
            const file = e.target.files?.[0]
            if (file) void importJson(file)
            e.target.value = ''
          }}
        />
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
