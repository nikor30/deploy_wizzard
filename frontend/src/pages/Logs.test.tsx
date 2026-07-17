import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import Logs from './Logs'

const logPage = {
  total: 2,
  entries: [
    {
      id: 1,
      timestamp: '2026-07-17T10:00:00+00:00',
      level: 'ERROR',
      component: 'app.services.day0',
      message: 'Day-0 failed for device',
      job_id: 4,
      device_serial: 'FCW1',
      context: { ccc_device_id: 'pnp-1', error: 'PnP onboarding failed' },
    },
    {
      id: 2,
      timestamp: '2026-07-17T10:01:00+00:00',
      level: 'INFO',
      component: 'app.api.wizard',
      message: 'Day-0 started',
      job_id: 4,
      device_serial: null,
      context: null,
    },
  ],
}

const deliveries = [
  {
    id: 9,
    job_id: 4,
    device_serial: 'FCW1',
    status: 'failed',
    attempts: 4,
    last_error: 'HTTP 500',
    created_at: '2026-07-17T10:02:00+00:00',
    payload: { event: 'day0_success' },
  },
]

let fetchMock: Mock

beforeEach(() => {
  fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    if (url.includes('/retry') && init?.method === 'POST')
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ ...deliveries[0], status: 'delivered', attempts: 5 }),
      })
    if (url.startsWith('/api/logs/webhook-deliveries'))
      return Promise.resolve({ ok: true, json: () => Promise.resolve(deliveries) })
    return Promise.resolve({ ok: true, json: () => Promise.resolve(logPage) })
  })
  vi.stubGlobal('fetch', fetchMock)
})

describe('Logs', () => {
  it('lists entries and expands context', async () => {
    render(<Logs />)
    expect(await screen.findByText('Day-0 failed for device')).toBeInTheDocument()
    expect(screen.getByText('2 entries')).toBeInTheDocument()
    await userEvent.click(screen.getByText('Day-0 failed for device'))
    expect(screen.getByText(/"ccc_device_id": "pnp-1"/)).toBeInTheDocument()
  })

  it('applies the level filter as a query parameter', async () => {
    render(<Logs />)
    await screen.findByText('Day-0 started')
    await userEvent.selectOptions(screen.getByLabelText('Level'), 'error')
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) => (url as string).includes('level=error'))
      expect(call).toBeDefined()
    })
  })

  it('retries a failed webhook delivery', async () => {
    render(<Logs />)
    const button = await screen.findByRole('button', { name: 'Retry webhook' })
    await userEvent.click(button)
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Retry webhook' })).not.toBeInTheDocument(),
    )
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          (url as string) === '/api/logs/webhook-deliveries/9/retry' &&
          (init as RequestInit)?.method === 'POST',
      ),
    ).toBe(true)
  })
})
