import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { ENV_ITEM, listItem, overviewCounts, reportListItem } from '@/test/fixtures'
import { mockFetch, renderRoute, requestsTo } from '@/test/render'

const page = (items: unknown[], next: number | null = null) => ({
  body: { items, next_offset: next },
})

function backend(overrides: Record<string, unknown> = {}) {
  return mockFetch({
    'GET /health': { body: { status: 'ok', database: 'ok' } },
    'GET /overview': { body: overviewCounts },
    'GET /reports': { body: { items: [reportListItem], next_offset: null } },
    'GET /investigations': page([listItem]),
    'GET /environments': { body: [ENV_ITEM] },
    ...overrides,
  })
}

describe('routing', () => {
  it.each([
    ['/investigations', 'Investigations'],
    ['/investigations/new', 'New investigation'],
    ['/environments', 'Environments'],
    ['/reports', 'Reports'],
    ['/organizations', 'Organizations'],
    ['/settings', 'Settings'],
  ])('renders %s lazily inside the shell', async (path, heading) => {
    backend()
    renderRoute(path)
    expect(await screen.findByRole('heading', { level: 1, name: heading })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Skip to content' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /switch organization/i })).toBeInTheDocument()
  })

  it('renders the overview with stored counts and navigation', async () => {
    backend()
    renderRoute('/')
    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument()
    expect(await screen.findByText(/API ok · database ok/)).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: /Investigations 11/ })).toHaveAttribute(
      'href',
      '/investigations',
    )
    expect(screen.getByRole('link', { name: /Report versions 5/ })).toHaveAttribute(
      'href',
      '/reports',
    )
  })

  it('shows not found for an environment id that cannot exist', async () => {
    const fetchMock = backend()
    renderRoute('/environments/abc')
    expect(await screen.findByText('Environment not found')).toBeInTheDocument()
    expect(requestsTo(fetchMock, 'GET', '/environments/abc')).toHaveLength(0)
  })

  it('lists environments without configuration', async () => {
    backend()
    renderRoute('/environments')
    const list = await screen.findByRole('list', { name: 'Environments' })
    expect(within(list).getByText('ubuntu-vm')).toBeInTheDocument()
    expect(within(list).getByText(/Local · 3 investigations/)).toBeInTheDocument()
  })

  it('pages through the investigation history', async () => {
    const fetchMock = backend({
      'GET /investigations?limit=20&offset=0': page([listItem], 20),
      'GET /investigations?limit=20&offset=20': page([
        {
          ...listItem,
          id: '00000000-0000-4000-8000-000000000001',
          title: 'Older one',
          latest_report_version: null,
        },
      ]),
    })
    renderRoute('/investigations')
    const list = await screen.findByRole('list', { name: 'Investigation history' })
    expect(within(list).getAllByRole('link')).toHaveLength(1)

    await userEvent.click(screen.getByRole('button', { name: 'Load more' }))
    expect(await within(list).findByText('Older one')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument()
    expect(requestsTo(fetchMock, 'GET', '/investigations')).toHaveLength(2)
  })

  it('shows an empty history with a way to start', async () => {
    backend({ 'GET /investigations': page([]) })
    renderRoute('/investigations')
    expect(await screen.findByText('No investigations yet')).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: /new investigation/i }).length).toBeGreaterThan(0)
  })

  it('renders a not-found page for unknown routes', async () => {
    renderRoute('/does-not-exist')
    expect(
      await screen.findByRole('heading', { level: 1, name: 'Page not found' }),
    ).toBeInTheDocument()
  })

  it('marks the active navigation item', async () => {
    backend()
    renderRoute('/reports')
    await screen.findByRole('heading', { level: 1, name: 'Reports' })
    const nav = screen.getByRole('navigation', { name: 'Main' })
    expect(within(nav).getByRole('link', { name: 'Reports' })).toHaveAttribute(
      'aria-current',
      'page',
    )
    expect(within(nav).getByRole('link', { name: 'Overview' })).not.toHaveAttribute('aria-current')
  })

  it('shows retryable errors when the backend is unreachable', async () => {
    mockFetch({})
    renderRoute('/investigations')
    expect(await screen.findByText('Backend unreachable')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })
})
