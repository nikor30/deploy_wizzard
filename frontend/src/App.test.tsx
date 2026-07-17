import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import App from './App'

describe('App shell', () => {
  it('renders the sidebar navigation and the wizard placeholder', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>,
    )
    expect(screen.getByText('PnP Bridge')).toBeInTheDocument()
    for (const label of ['Wizard', 'Statistics', 'Logs', 'Credentials', 'Site Mapping']) {
      expect(screen.getByRole('link', { name: label })).toBeInTheDocument()
    }
    expect(screen.getByRole('heading', { name: 'Onboarding Wizard' })).toBeInTheDocument()
  })

  it('renders placeholder pages for unbuilt phases', () => {
    render(
      <MemoryRouter initialEntries={['/stats']}>
        <App />
      </MemoryRouter>,
    )
    expect(screen.getByRole('heading', { name: 'Statistics' })).toBeInTheDocument()
    expect(screen.getByText(/Coming in phase P6/)).toBeInTheDocument()
  })
})
