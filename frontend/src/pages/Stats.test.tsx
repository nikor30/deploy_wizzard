import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import Stats from './Stats'

const payload = {
  days: 30,
  totals: { jobs: 3, devices: 6, claimed: 5, provisioned: 4, failed: 1 },
  success_rate: 0.833,
  avg_day0_seconds: 45.0,
  avg_dayn_seconds: 120.0,
  failures_by_category: { timeout: 1 },
  jobs_over_time: [
    { date: '2026-07-16', jobs: 2, succeeded: 3, failed: 1 },
    { date: '2026-07-17', jobs: 1, succeeded: 2, failed: 0 },
  ],
}

let fetchMock: Mock

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(payload) })
  vi.stubGlobal('fetch', fetchMock)
})

describe('Stats', () => {
  it('renders tiles and charts from the API payload', async () => {
    render(<Stats />)
    expect(await screen.findByText('83%')).toBeInTheDocument()
    expect(screen.getByText('Provisioned')).toBeInTheDocument()
    expect(screen.getByText('45s / 2.0min')).toBeInTheDocument()
    expect(
      screen.getByRole('img', { name: 'Devices succeeded and failed per day' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Failures by error category' })).toBeInTheDocument()
    expect(screen.getByText('timeout')).toBeInTheDocument()
  })

  it('re-queries when the time range changes', async () => {
    render(<Stats />)
    await screen.findByText('83%')
    await userEvent.selectOptions(screen.getByRole('combobox'), '7')
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([url]) => url === '/api/stats?days=7')).toBe(true),
    )
  })
})
