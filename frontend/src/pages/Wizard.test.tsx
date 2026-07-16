import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import Wizard from './Wizard'

const pnpDevices = [
  {
    ccc_device_id: 'pnp-1',
    serial: 'FCW1234ABCD',
    pid: 'C9300-48P',
    state: 'Unclaimed',
    ip_address: '10.1.1.5',
    last_contact: null,
  },
  {
    ccc_device_id: 'pnp-2',
    serial: 'FCW5678EFGH',
    pid: 'C9200-24T',
    state: 'Unclaimed',
    ip_address: null,
    last_contact: null,
  },
]

const matchedJob = {
  id: 7,
  status: 'in_progress',
  current_step: 2,
  created_at: '2026-07-16T12:00:00+00:00',
  device_count: 2,
  devices: [
    {
      id: 71,
      serial: 'FCW1234ABCD',
      pid: 'C9300-48P',
      ccc_device_id: 'pnp-1',
      match_status: 'matched',
      netbox_name: 'sw-ffm-01',
      netbox_site_name: 'FFM-DC1',
      ccc_site_name: 'Global/Germany/Frankfurt/DC1',
      mgmt_ip: '172.20.10.5/24',
      mgmt_vlan: null,
      vlan_options: [{ id: 5, vid: 110, name: 'MGMT' }],
    },
    {
      id: 72,
      serial: 'FCW5678EFGH',
      pid: 'C9200-24T',
      ccc_device_id: 'pnp-2',
      match_status: 'unmatched',
      netbox_name: null,
      netbox_site_name: null,
      ccc_site_name: null,
      mgmt_ip: null,
      mgmt_vlan: null,
      vlan_options: [],
    },
  ],
}

const templates = [{ id: 'tmpl-1', name: 'Day0-Onboarding', project: 'Onboarding' }]

const runningJob = {
  ...matchedJob,
  status: 'day0_running',
  current_step: 3,
  devices: matchedJob.devices.map((d) =>
    d.match_status === 'matched' ? { ...d, state: 'queued' } : d,
  ),
}

const finishedJob = {
  ...matchedJob,
  status: 'day0_complete',
  current_step: 3,
  devices: matchedJob.devices.map((d) =>
    d.match_status === 'matched' ? { ...d, state: 'success' } : d,
  ),
}

function jsonResponse(body: unknown): Response {
  return { ok: true, json: () => Promise.resolve(body) } as Response
}

let fetchMock: Mock

beforeEach(() => {
  fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    if (url === '/api/wizard/jobs' && !init?.method) return Promise.resolve(jsonResponse([]))
    if (url === '/api/wizard/pnp-devices') return Promise.resolve(jsonResponse(pnpDevices))
    if (url === '/api/wizard/day0/templates') return Promise.resolve(jsonResponse(templates))
    if (url === '/api/wizard/jobs' && init?.method === 'POST')
      return Promise.resolve(jsonResponse({ ...matchedJob, devices: [] }))
    if (url.endsWith('/match')) return Promise.resolve(jsonResponse(matchedJob))
    if (url.endsWith('/claim')) return Promise.resolve(jsonResponse(runningJob))
    if (url === '/api/wizard/jobs/7') return Promise.resolve(jsonResponse(finishedJob))
    if (init?.method === 'PUT')
      return Promise.resolve(jsonResponse({ ...matchedJob.devices[0], mgmt_vlan: 110 }))
    return Promise.resolve(jsonResponse({}))
  })
  vi.stubGlobal('fetch', fetchMock)
})

function renderWizard() {
  return render(
    <MemoryRouter>
      <Wizard />
    </MemoryRouter>,
  )
}

describe('Wizard', () => {
  it('walks step 1 → 2: select devices, create job, show match review', async () => {
    renderWizard()
    await userEvent.click(screen.getByRole('button', { name: 'Start new onboarding job' }))

    // Step 1: device table with selection gating
    const continueButton = await screen.findByRole('button', { name: /Continue with 0/ })
    expect(continueButton).toBeDisabled()
    await userEvent.click(await screen.findByLabelText('Select FCW1234ABCD'))
    await userEvent.click(screen.getByLabelText('Select FCW5678EFGH'))
    const enabled = screen.getByRole('button', { name: /Continue with 2/ })
    expect(enabled).toBeEnabled()
    await userEvent.click(enabled)

    // Step 2: match results
    expect(await screen.findByText('sw-ffm-01', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('matched')).toBeInTheDocument()
    expect(screen.getByText('no NetBox match')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Continue to Day-0 claim \(1 device/ })).toBeEnabled()
  })

  it('runs Day-0: template pick, start claim, live progress to summary', async () => {
    renderWizard()
    await userEvent.click(screen.getByRole('button', { name: 'Start new onboarding job' }))
    await userEvent.click(await screen.findByLabelText('Select FCW1234ABCD'))
    await userEvent.click(screen.getByRole('button', { name: /Continue with 1/ }))
    await userEvent.click(await screen.findByRole('button', { name: /Continue to Day-0 claim/ }))

    // Step 3: start disabled until a template is picked
    const startButton = await screen.findByRole('button', { name: /Start Day-0 claim/ })
    expect(startButton).toBeDisabled()
    await userEvent.selectOptions(screen.getByLabelText(/Onboarding template/i), 'tmpl-1')
    expect(startButton).toBeEnabled()
    await userEvent.click(startButton)

    const claimCall = fetchMock.mock.calls.find(([url]) => (url as string).endsWith('/claim'))
    expect(JSON.parse((claimCall![1] as RequestInit).body as string)).toEqual({
      config_id: 'tmpl-1',
      image_id: null,
    })

    // Polling fallback (no EventSource in jsdom) picks up the terminal snapshot
    expect(await screen.findByText('queued')).toBeInTheDocument()
    expect(await screen.findByText('success', {}, { timeout: 4000 })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Day-0 finished: 1 succeeded, 0 failed.')
  })

  it('resumes a day0 job directly into step 3', async () => {
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url === '/api/wizard/jobs' && !init?.method)
        return Promise.resolve(jsonResponse([finishedJob]))
      if (url === '/api/wizard/day0/templates') return Promise.resolve(jsonResponse(templates))
      return Promise.resolve(jsonResponse(finishedJob))
    })
    renderWizard()
    await userEvent.click(await screen.findByRole('button', { name: 'Resume' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Day-0 finished')
  })

  it('filters the device table by serial', async () => {
    renderWizard()
    await userEvent.click(screen.getByRole('button', { name: 'Start new onboarding job' }))
    await screen.findByLabelText('Select FCW1234ABCD')
    await userEvent.type(screen.getByPlaceholderText('Filter by serial or PID…'), '5678')
    expect(screen.queryByLabelText('Select FCW1234ABCD')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Select FCW5678EFGH')).toBeInTheDocument()
  })

  it('selects a mgmt VLAN on a matched device', async () => {
    renderWizard()
    await userEvent.click(screen.getByRole('button', { name: 'Start new onboarding job' }))
    await userEvent.click(await screen.findByLabelText('Select FCW1234ABCD'))
    await userEvent.click(screen.getByRole('button', { name: /Continue with 1/ }))

    const select = await screen.findByLabelText(/Mgmt VLAN/i)
    await userEvent.selectOptions(select, '110')
    await waitFor(() => {
      const putCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')
      expect(putCall).toBeDefined()
      expect(JSON.parse((putCall![1] as RequestInit).body as string)).toEqual({ mgmt_vlan: 110 })
    })
  })

  it('offers resuming an existing job from the start view', async () => {
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url === '/api/wizard/jobs' && !init?.method)
        return Promise.resolve(jsonResponse([matchedJob]))
      if (url.endsWith('/match')) return Promise.resolve(jsonResponse(matchedJob))
      return Promise.resolve(jsonResponse({}))
    })
    renderWizard()
    await userEvent.click(await screen.findByRole('button', { name: 'Resume' }))
    expect(await screen.findByText('sw-ffm-01', { exact: false })).toBeInTheDocument()
  })
})
