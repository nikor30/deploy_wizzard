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
