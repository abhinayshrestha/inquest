import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { investigation, INVESTIGATION_ID } from '@/test/fixtures'
import { mockFetch, renderRoute } from '@/test/render'

describe('routing', () => {
  it.each([
    ['/investigations', 'Investigations'],
    ['/environments', 'Environments'],
    ['/environments/abc', 'Environment'],
    ['/reports', 'Reports'],
    ['/organizations', 'Organizations'],
    ['/settings', 'Settings'],
  ])('renders %s lazily inside the shell', async (path, heading) => {
    renderRoute(path)
    expect(await screen.findByRole('heading', { level: 1, name: heading })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Skip to content' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /switch organization/i })).toBeInTheDocument()
  })

  it('renders the overview with the backend health check', async () => {
    mockFetch({ 'GET /health': { body: { status: 'ok', database: 'ok' } } })
    renderRoute('/')
    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument()
    expect(await screen.findByText(/API ok · database ok/)).toBeInTheDocument()
  })

  it('shows backend gaps instead of invented data', async () => {
    renderRoute('/environments')
    await screen.findByRole('heading', { level: 1, name: 'Environments' })
    expect(screen.getByText('GET /environments')).toBeInTheDocument()
  })

  it('renders a not-found page for unknown routes', async () => {
    renderRoute('/does-not-exist')
    expect(
      await screen.findByRole('heading', { level: 1, name: 'Page not found' }),
    ).toBeInTheDocument()
  })

  it('marks the active navigation item', async () => {
    renderRoute('/reports')
    await screen.findByRole('heading', { level: 1, name: 'Reports' })
    const nav = screen.getByRole('navigation', { name: 'Main' })
    expect(within(nav).getByRole('link', { name: 'Reports' })).toHaveAttribute(
      'aria-current',
      'page',
    )
    expect(within(nav).getByRole('link', { name: 'Overview' })).not.toHaveAttribute('aria-current')
  })

  it('opens an investigation by ID from the development form', async () => {
    mockFetch({ [`GET /investigations/${INVESTIGATION_ID}`]: { body: investigation } })
    const { router } = renderRoute('/investigations')
    const user = userEvent.setup()
    const input = await screen.findByLabelText('Investigation ID')

    await user.type(input, 'not-a-uuid')
    await user.click(screen.getByRole('button', { name: 'Open' }))
    expect(screen.getByText(/valid investigation ID/)).toBeInTheDocument()

    await user.clear(input)
    await user.type(input, INVESTIGATION_ID)
    await user.click(screen.getByRole('button', { name: 'Open' }))
    await waitFor(() =>
      expect(router.state.location.pathname).toBe(`/investigations/${INVESTIGATION_ID}`),
    )
  })
})

describe('investigation detail', () => {
  it('shows a skeleton, then the investigation summary', async () => {
    mockFetch({ [`GET /investigations/${INVESTIGATION_ID}`]: { body: investigation } })
    renderRoute(`/investigations/${INVESTIGATION_ID}`)
    expect(
      await screen.findByRole('heading', { level: 1, name: 'API latency on /slow' }),
    ).toBeInTheDocument()
    expect(screen.getByText('idle')).toBeInTheDocument()
    expect(screen.getByText('Version 1')).toBeInTheDocument()
    expect(screen.getByText(/Environment: ubuntu-vm/)).toBeInTheDocument()
  })

  it('shows not found for a 404', async () => {
    mockFetch({
      [`GET /investigations/${INVESTIGATION_ID}`]: {
        status: 404,
        body: { detail: 'investigation not found' },
      },
    })
    renderRoute(`/investigations/${INVESTIGATION_ID}`)
    expect(await screen.findByText('Investigation not found')).toBeInTheDocument()
  })

  it('does not request an ID that cannot exist', async () => {
    const fetchMock = mockFetch({})
    renderRoute('/investigations/not-a-uuid')
    expect(await screen.findByText('Investigation not found')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('shows a retryable error when the backend is unreachable', async () => {
    mockFetch({})
    renderRoute(`/investigations/${INVESTIGATION_ID}`)
    expect(await screen.findByText('Backend unreachable')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })
})
