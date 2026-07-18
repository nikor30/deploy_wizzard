import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import SettingsMapping from './SettingsMapping'

const netboxSites = [
  { id: 1, name: 'FFM-DC1', slug: 'ffm-dc1' },
  { id: 2, name: 'BER-DC1', slug: 'ber-dc1' },
]

const cccSites = [
  { id: 'uuid-1', name_hierarchy: 'Global/Germany/Frankfurt/DC1' },
  { id: 'uuid-2', name_hierarchy: 'Global/Germany/Berlin/DC1' },
]

const storedMappings = {
  mappings: [
    {
      netbox_site_id: 2,
      netbox_site_name: 'BER-DC1',
      ccc_site_id: 'uuid-2',
      ccc_site_name: 'Global/Germany/Berlin/DC1',
    },
  ],
}

let fetchMock: Mock

function jsonResponse(body: unknown): Response {
  return { ok: true, json: () => Promise.resolve(body) } as Response
}

beforeEach(() => {
  fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url === '/api/mappings/sources/netbox') return Promise.resolve(jsonResponse(netboxSites))
    if (url === '/api/mappings/sources/ccc') return Promise.resolve(jsonResponse(cccSites))
    return Promise.resolve(jsonResponse(storedMappings))
  })
  vi.stubGlobal('fetch', fetchMock)
})

describe('SettingsMapping', () => {
  it('shows both site columns and flags unmapped NetBox sites', async () => {
    render(<SettingsMapping />)
    expect(await screen.findByRole('button', { name: /FFM-DC1/ })).toBeInTheDocument()
    expect(screen.getByText('Global/Germany/Frankfurt/DC1')).toBeInTheDocument()
    // FFM-DC1 is unmapped, BER-DC1 is mapped
    expect(screen.getByText('unmapped')).toBeInTheDocument()
    expect(screen.getByText('Mappings (1)')).toBeInTheDocument()
  })

  it('pairs a NetBox site with a CCC site and saves', async () => {
    render(<SettingsMapping />)
    await userEvent.click(await screen.findByRole('button', { name: /FFM-DC1/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Global/Germany/Frankfurt/DC1' }))
    expect(screen.getByText('Mappings (2)')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Save mappings' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Mappings saved.'))

    const putCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')
    expect(putCall).toBeDefined()
    const body = JSON.parse((putCall![1] as RequestInit).body as string)
    expect(body.mappings).toHaveLength(2)
    expect(body.mappings).toContainEqual({
      netbox_site_id: 1,
      netbox_site_name: 'FFM-DC1',
      ccc_site_id: 'uuid-1',
      ccc_site_name: 'Global/Germany/Frankfurt/DC1',
    })
  })

  it('removes a mapping', async () => {
    render(<SettingsMapping />)
    await screen.findByText('Mappings (1)')
    await userEvent.click(screen.getByRole('button', { name: 'Remove' }))
    expect(screen.getByText('Mappings (0)')).toBeInTheDocument()
  })

  it('shows an alert when sources cannot be loaded', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.startsWith('/api/mappings/sources')) {
        return Promise.resolve({
          ok: false,
          json: () => Promise.resolve({ detail: 'NetBox is not configured.' }),
        } as Response)
      }
      return Promise.resolve(jsonResponse(storedMappings))
    })
    render(<SettingsMapping />)
    expect(await screen.findByRole('alert')).toHaveTextContent('NetBox is not configured.')
  })
})

describe('SettingsMapping suggestions', () => {
  it('pre-fills suggested pairs with a confidence badge for review', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url === '/api/mappings/sources/netbox') return Promise.resolve(jsonResponse(netboxSites))
      if (url === '/api/mappings/sources/ccc') return Promise.resolve(jsonResponse(cccSites))
      if (url === '/api/mappings/sites/suggest')
        return Promise.resolve(
          jsonResponse([
            {
              netbox_site_id: 1,
              netbox_site_name: 'FFM-DC1',
              ccc_site_id: 'uuid-1',
              ccc_site_name: 'Global/Germany/Frankfurt/DC1',
              confidence: 0.82,
            },
          ]),
        )
      return Promise.resolve(jsonResponse(storedMappings))
    })
    render(<SettingsMapping />)
    await screen.findByRole('button', { name: /FFM-DC1/ })
    await userEvent.click(screen.getByRole('button', { name: 'Suggest mappings' }))

    expect(await screen.findByText('Mappings (2)')).toBeInTheDocument()
    expect(screen.getByText('suggested · 82%')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Suggested 1 mapping(s)')
    // the suggestion can be corrected: remove it again
    await userEvent.click(screen.getAllByRole('button', { name: 'Remove' })[1])
    expect(screen.getByText('Mappings (1)')).toBeInTheDocument()
  })

  it('reports when nothing can be pre-matched', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url === '/api/mappings/sources/netbox') return Promise.resolve(jsonResponse(netboxSites))
      if (url === '/api/mappings/sources/ccc') return Promise.resolve(jsonResponse(cccSites))
      if (url === '/api/mappings/sites/suggest') return Promise.resolve(jsonResponse([]))
      return Promise.resolve(jsonResponse(storedMappings))
    })
    render(<SettingsMapping />)
    await screen.findByRole('button', { name: /FFM-DC1/ })
    await userEvent.click(screen.getByRole('button', { name: 'Suggest mappings' }))
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('No confident matches'),
    )
    expect(screen.getByText('Mappings (1)')).toBeInTheDocument()
  })
})
