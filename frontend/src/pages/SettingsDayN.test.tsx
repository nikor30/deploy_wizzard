import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import SettingsDayN from './SettingsDayN'

const stored = {
  mappings: [{ variable: 'SNMP_LOCATION', source_path: 'device.custom_fields.snmp_location' }],
}

let fetchMock: Mock

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(stored) })
  vi.stubGlobal('fetch', fetchMock)
})

describe('SettingsDayN', () => {
  it('lists stored mappings and saves an added row', async () => {
    render(<SettingsDayN />)
    expect(await screen.findByDisplayValue('SNMP_LOCATION')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Add mapping' }))
    await userEvent.type(screen.getByLabelText('Variable 2'), 'NTP_SERVER')
    await userEvent.type(
      screen.getByLabelText('Source path 2'),
      'device.config_context.ntp.servers.0',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Save mappings' }))

    await waitFor(() => {
      const putCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')
      expect(putCall).toBeDefined()
      const body = JSON.parse((putCall![1] as RequestInit).body as string)
      expect(body.mappings).toContainEqual({
        variable: 'NTP_SERVER',
        source_path: 'device.config_context.ntp.servers.0',
      })
    })
  })

  it('removes a row locally', async () => {
    render(<SettingsDayN />)
    await screen.findByDisplayValue('SNMP_LOCATION')
    await userEvent.click(screen.getByRole('button', { name: 'Remove' }))
    expect(screen.queryByDisplayValue('SNMP_LOCATION')).not.toBeInTheDocument()
  })
})

describe('SettingsDayN suggestions', () => {
  it('suggests paths for a template and keeps unmatched variables manual', async () => {
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url === '/api/wizard/day0/templates')
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve([{ id: 'tpl-dayn', name: 'DayN Baseline', project: 'Baseline' }]),
        })
      if (url === '/api/settings/dayn/suggest' && init?.method === 'POST')
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve([
              { variable: 'HOSTNAME', source_path: 'device.name', confidence: 0.9 },
              { variable: 'RADIUS_KEY', source_path: null, confidence: 0 },
            ]),
        })
      return Promise.resolve({ ok: true, json: () => Promise.resolve(stored) })
    })
    render(<SettingsDayN />)
    await screen.findByDisplayValue('SNMP_LOCATION')

    await userEvent.selectOptions(screen.getByLabelText('Template for suggestions'), 'tpl-dayn')
    await userEvent.click(screen.getByRole('button', { name: 'Suggest mappings' }))

    expect(await screen.findByDisplayValue('device.name')).toBeInTheDocument()
    expect(screen.getByDisplayValue('HOSTNAME')).toBeInTheDocument()
    expect(screen.getByText('suggested · 90%')).toBeInTheDocument()
    // unmatched variable appears as a row with an empty path (manual entry)
    expect(screen.getByDisplayValue('RADIUS_KEY')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Suggested 1 of 2')

    const body = JSON.parse(
      (fetchMock.mock.calls.find(([, i]) => i?.method === 'POST')![1] as RequestInit)
        .body as string,
    )
    expect(body).toEqual({ template_id: 'tpl-dayn' })
  })
})

describe('SettingsDayN template secrets', () => {
  it('lists masked secrets, stores a new one, and deletes', async () => {
    let secrets = [{ name: 'radius_key', secret_masked: '****-123' }]
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url === '/api/settings/secrets')
        return Promise.resolve({ ok: true, json: () => Promise.resolve(secrets) })
      if (url === '/api/settings/secrets/tacacs_key' && init?.method === 'PUT')
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ name: 'tacacs_key', secret_masked: '****cs99' }),
        })
      if (url === '/api/settings/secrets/radius_key' && init?.method === 'DELETE') {
        secrets = []
        return Promise.resolve({ ok: true, json: () => Promise.resolve(null) })
      }
      if (url === '/api/wizard/day0/templates')
        return Promise.resolve({ ok: true, json: () => Promise.resolve([]) })
      return Promise.resolve({ ok: true, json: () => Promise.resolve(stored) })
    })
    render(<SettingsDayN />)

    expect(await screen.findByText('secret.radius_key')).toBeInTheDocument()
    expect(screen.getByText('****-123')).toBeInTheDocument()

    await userEvent.type(screen.getByLabelText('Secret name'), 'tacacs_key')
    await userEvent.type(screen.getByLabelText('Secret value'), 'tacacs99')
    await userEvent.click(screen.getByRole('button', { name: 'Store secret' }))
    expect(await screen.findByText('secret.tacacs_key')).toBeInTheDocument()
    expect(screen.getByText('****cs99')).toBeInTheDocument()
    // the plaintext never renders anywhere
    expect(screen.queryByText(/tacacs99/)).not.toBeInTheDocument()
    const putCall = fetchMock.mock.calls.find(
      ([u, i]) => u === '/api/settings/secrets/tacacs_key' && i?.method === 'PUT',
    )
    expect(JSON.parse((putCall![1] as RequestInit).body as string)).toEqual({
      secret: 'tacacs99',
    })

    await userEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0])
    await waitFor(() => expect(screen.queryByText('secret.radius_key')).not.toBeInTheDocument())
  })
})
