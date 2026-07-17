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
