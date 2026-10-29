import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { ENV, ENV_ITEM, INVESTIGATION_ID, listItem, ORGANIZATION_ID } from '@/test/fixtures'
import { mockFetch, renderRoute, requestsTo, type Reply } from '@/test/render'
import { TEST_USER } from '@/test/session'

const PATH = `/environments/${ENV.id}`

const detail = {
  ...ENV,
  organization_id: ORGANIZATION_ID,
  owner_user_id: TEST_USER.id,
  credentials: 'process_development',
  created_at: '2026-10-17T10:00:00Z',
  updated_at: '2026-10-18T10:00:00Z',
  scope: {
    status: 'configured',
    hosts: ['abbyvm'],
    services: ['inquest-demo'],
    deployment_environment: null,
    problems: [
      {
        code: 'policy.scope_ambiguous',
        message: "host 'abbyvm' is also claimed by another active environment; queries for it are rejected",
      },
    ],
  },
  stats: { investigations: 3, active_investigations: 1, evidence: 14, tool_calls: 27, last_investigation_at: '2026-10-21T17:30:00Z' },
  telemetry_provider: { provider: 'signoz', source: 'process_environment', configured: true, issues: [] },
  ai_provider: {
    provider: 'nebius_token_factory', source: 'process_environment', configured: false,
    issues: ['NEBIUS_LOCAL_MODEL'], main_model: 'nvidia/Nemotron-3-Ultra-550b-a55b', local_model: null,
  },
  per_environment_credentials: 'not_available',
} // prettier-ignore

const coverage = {
  signals: [
    { signal: 'logs', identity: 'service', value: 'inquest-demo', status: 'no_data', window_start: '2026-10-21T17:16:00Z', window_end: '2026-10-21T17:46:00Z', checked_at: '2026-10-21T17:46:10Z', investigation_id: INVESTIGATION_ID },
    { signal: 'metrics', identity: 'host', value: 'abbyvm', status: 'observed', window_start: '2026-10-21T17:16:00Z', window_end: '2026-10-21T17:46:00Z', checked_at: '2026-10-21T17:46:20Z', investigation_id: INVESTIGATION_ID },
  ],
  queries_checked: 12,
  queries_failed: 1,
} // prettier-ignore

const NO_CREDENTIALS = ['nebius', 'signoz'].map((kind) => ({
  kind, configured: false, state: 'not_configured', updated_at: null, updated_by: null,
  main_model: null, local_model: null, isolation: null,
})) // prettier-ignore

function backend(extra: Record<string, Reply | Reply[]> = {}) {
  return mockFetch({
    [`GET ${PATH}`]: { body: detail },
    [`GET ${PATH}/telemetry`]: { body: coverage },
    'GET /environments': { body: [ENV_ITEM] },
    'GET /investigations': { body: { items: [listItem], next_offset: null } },
    [`GET ${PATH}/credentials`]: { body: NO_CREDENTIALS },
    [`GET /organizations/${ORGANIZATION_ID}/members`]: { body: [] },
    ...extra,
  })
}

describe('environments', () => {
  it('lists environments with activity and links to their detail', async () => {
    backend()
    renderRoute('/environments')
    const list = await screen.findByRole('list', { name: 'Environments' })
    const link = within(list).getByRole('link', { name: /ubuntu-vm/ })
    expect(link).toHaveAttribute('href', PATH)
    expect(link).toHaveTextContent('3 investigations')
  })

  it('shows the overview with stats and only this environment’s investigations', async () => {
    const fetchMock = backend()
    renderRoute(PATH)
    expect(await screen.findByRole('heading', { level: 1, name: 'ubuntu-vm' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Overview', selected: true })).toBeInTheDocument()
    expect(screen.getByText('Evidence recorded').nextSibling).toHaveTextContent('14')

    const list = await screen.findByRole('list', { name: 'Investigations of ubuntu-vm' })
    expect(within(list).getByRole('link', { name: /API latency on \/slow/ })).toBeInTheDocument()
    const [input] = requestsTo(fetchMock, 'GET', '/investigations')[0]!
    expect(String(input)).toContain(`environment_id=${ENV.id}`)
    expect(screen.getByRole('link', { name: /new investigation/i })).toHaveAttribute(
      'href',
      `/investigations/new?environment=${ENV.id}`,
    )
  })

  it('keeps the selected section in the URL', async () => {
    backend()
    const { router } = renderRoute(`${PATH}?tab=ai`)
    expect(
      await screen.findByRole('tab', { name: 'AI provider', selected: true }),
    ).toBeInTheDocument()

    await userEvent.click(screen.getByRole('tab', { name: 'Telemetry' }))
    expect(router.state.location.search).toBe('?tab=telemetry')
    await userEvent.click(screen.getByRole('tab', { name: 'Overview' }))
    expect(router.state.location.search).toBe('')
  })

  it('shows scope, scope problems and recorded availability without claiming live health', async () => {
    backend()
    renderRoute(`${PATH}?tab=telemetry`)
    expect(await screen.findByText('abbyvm', { selector: 'code' })).toBeInTheDocument()
    expect(
      within(screen.getByRole('list', { name: 'Scope problems' })).getByText(/also claimed/),
    ).toBeInTheDocument()

    const table = await screen.findByRole('table', { name: /availability as last recorded/i })
    const rows = within(table)
      .getAllByRole('row')
      .slice(1)
      .map((r) => r.textContent)
    expect(rows[0]).toMatch(/logs.*inquest-demo.*No data in window/)
    expect(rows[1]).toMatch(/metrics.*abbyvm.*Data observed/)
    expect(screen.getByText(/not a live health check/)).toBeInTheDocument()
    expect(screen.getByText(/Based on 12 successful queries; 1 queries failed/)).toBeInTheDocument()
  })

  it('explains when nothing has been recorded yet', async () => {
    backend({
      [`GET ${PATH}/telemetry`]: { body: { signals: [], queries_checked: 0, queries_failed: 0 } },
    })
    renderRoute(`${PATH}?tab=telemetry`)
    expect(await screen.findByText('Nothing recorded yet')).toBeInTheDocument()
  })

  it('shows provider status from the global configuration and names missing settings', async () => {
    backend()
    renderRoute(`${PATH}?tab=ai`)
    expect(await screen.findByText('nvidia/Nemotron-3-Ultra-550b-a55b')).toBeInTheDocument()
    expect(await screen.findByText('Not configured')).toBeInTheDocument()
    expect(screen.getByText(/Missing or invalid settings: NEBIUS_LOCAL_MODEL/)).toBeInTheDocument()
    expect(screen.getAllByText(/development settings/).length).toBeGreaterThan(0)
  })

  it('shows the owner and, with nobody to hand it to, says so', async () => {
    backend()
    renderRoute(`${PATH}?tab=settings`)
    // The fixture's owner is the signed-in test user.
    expect(await screen.findByText('you')).toBeInTheDocument()
    expect(await screen.findByText(/No other members to transfer it to/)).toBeInTheDocument()
  })

  it('handles unknown environments and retries failures', async () => {
    backend({
      [`GET ${PATH}`]: [
        { status: 503, body: { detail: 'database unavailable' } },
        { body: detail },
      ],
    })
    renderRoute(PATH)
    await userEvent.click(await screen.findByRole('button', { name: /try again/i }))
    expect(await screen.findByRole('heading', { level: 1, name: 'ubuntu-vm' })).toBeInTheDocument()
  })

  it('shows not found for a missing environment', async () => {
    backend({ [`GET ${PATH}`]: { status: 404, body: { detail: 'environment not found' } } })
    renderRoute(PATH)
    expect(await screen.findByText('Environment not found')).toBeInTheDocument()
  })

  it('preselects the environment a new investigation was started from', async () => {
    backend()
    renderRoute(`/investigations/new?environment=${ENV.id}`)
    await waitFor(() => expect(screen.getByLabelText('Environment')).toHaveValue(ENV.id))
  })
})
