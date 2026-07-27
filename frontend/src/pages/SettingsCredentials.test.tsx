import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import SettingsCredentials from './SettingsCredentials'

const storedCredentials = {
  catalyst: {
    base_url: 'https://ccc.example.com',
    username: 'admin',
    secret_masked: '****9999',
    tls_verify: false,
    enabled: true,
    configured: true,
  },
  netbox: {
    base_url: 'https://netbox.example.com',
    username: null,
    secret_masked: '****8888',
    tls_verify: true,
    enabled: true,
    configured: true,
  },
  webhook: {
    base_url: null,
    username: null,
    secret_masked: null,
    tls_verify: true,
    enabled: true,
    configured: false,
  },
}

function jsonResponse(body: unknown): Response {
  return { ok: true, json: () => Promise.resolve(body) } as Response
}

let fetchMock: Mock

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue(jsonResponse(storedCredentials))
  vi.stubGlobal('fetch', fetchMock)
})

describe('SettingsCredentials', () => {
  it('shows stored values with masked secrets as placeholders', async () => {
    render(<SettingsCredentials />)
    expect(await screen.findByDisplayValue('https://ccc.example.com')).toBeInTheDocument()
    expect(screen.getByLabelText('Password')).toHaveAttribute('placeholder', '****9999')
    expect(screen.getByLabelText('API token')).toHaveAttribute('placeholder', '****8888')
    // The real secret must never be present anywhere in the document.
    expect(document.body.innerHTML).not.toContain('9999x')
  })

  it('saves with secret=null when the secret field is untouched', async () => {
    render(<SettingsCredentials />)
    await screen.findByDisplayValue('https://ccc.example.com')

    await userEvent.click(screen.getByRole('button', { name: 'Save settings' }))

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Settings saved.'))
    const putCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')
    expect(putCall).toBeDefined()
    const body = JSON.parse((putCall![1] as RequestInit).body as string)
    expect(body.catalyst.secret).toBeNull()
    expect(body.netbox.secret).toBeNull()
  })

  it('shows the PnP state filter and saves a widened selection', async () => {
    fetchMock.mockImplementation((url: string) =>
      Promise.resolve(
        url === '/api/settings/flags'
          ? jsonResponse({
              debug: false,
              pnp_states: ['Unclaimed', 'Planned', 'Onboarding', 'Error'],
            })
          : jsonResponse(storedCredentials),
      ),
    )
    render(<SettingsCredentials />)

    const provisioned = await screen.findByLabelText('List Provisioned devices')
    expect(provisioned).not.toBeChecked()
    expect(screen.getByLabelText('List Unclaimed devices')).toBeChecked()

    await userEvent.click(provisioned)

    await waitFor(() => {
      const flagCall = fetchMock.mock.calls.find(
        ([url, init]) => url === '/api/settings/flags' && (init as RequestInit)?.method === 'PUT',
      )
      expect(flagCall).toBeDefined()
      expect(JSON.parse((flagCall![1] as RequestInit).body as string).pnp_states).toEqual([
        'Unclaimed',
        'Planned',
        'Onboarding',
        'Error',
        'Provisioned',
      ])
    })
  })

  it('keeps at least one PnP state selected', async () => {
    fetchMock.mockImplementation((url: string) =>
      Promise.resolve(
        url === '/api/settings/flags'
          ? jsonResponse({ debug: false, pnp_states: ['Unclaimed'] })
          : jsonResponse(storedCredentials),
      ),
    )
    render(<SettingsCredentials />)

    const unclaimed = await screen.findByLabelText('List Unclaimed devices')
    expect(unclaimed).toBeChecked()
    await userEvent.click(unclaimed)

    // unchecking the last state is a no-op — no PUT, still checked
    expect(unclaimed).toBeChecked()
    expect(
      fetchMock.mock.calls.filter(
        ([url, init]) => url === '/api/settings/flags' && (init as RequestInit)?.method === 'PUT',
      ),
    ).toHaveLength(0)
  })

  it('runs a connection test and shows the result', async () => {
    fetchMock.mockImplementation((url: string) =>
      Promise.resolve(
        url.includes('/test')
          ? jsonResponse({ ok: true, detail: 'Connected. 12 sites visible.' })
          : jsonResponse(storedCredentials),
      ),
    )
    render(<SettingsCredentials />)
    await screen.findByDisplayValue('https://ccc.example.com')

    const testButtons = screen.getAllByRole('button', { name: 'Test connection' })
    await userEvent.click(testButtons[0])

    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('Connected. 12 sites visible.'),
    )
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/settings/credentials/catalyst/test',
      expect.objectContaining({ method: 'POST' }),
    )
  })
})
