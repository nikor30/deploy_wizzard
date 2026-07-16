import { render, screen } from '@testing-library/react'
import { vi, describe, it, expect, beforeEach } from 'vitest'
import App from './App'

describe('App', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ status: 'ok', version: '0.1.0' }),
      }),
    )
  })

  it('renders the title and backend health', async () => {
    render(<App />)
    expect(screen.getByRole('heading', { name: 'PnP Bridge' })).toBeInTheDocument()
    expect(await screen.findByTestId('health')).toHaveTextContent('Backend ok · v0.1.0')
  })
})
