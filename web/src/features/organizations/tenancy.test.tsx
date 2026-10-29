import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { ENV, ENV_ITEM, INVESTIGATION_ID, investigation, ORGANIZATION_ID } from '@/test/fixtures'
import { MockEventSource } from '@/test/event-source'
import { mockFetch, renderRoute, requestsTo } from '@/test/render'
import { MEMBERSHIP, SIGNED_IN, TEST_USER } from '@/test/session'

// Phase 7B in the browser: pages show one organization's data, ask the server for that
// organization only, drop it when the organization changes and stop live views when
// access ends. The server is the boundary; these check the UI never blurs it.

const OTHER_ORG = '00000000-0000-4000-8000-0000000000f3'
const OTHER = { organization_id: OTHER_ORG, name: 'Other Org', slug: 'other-org', role: 'member' }
const TWO_ORGS = { body: { user: TEST_USER, memberships: [MEMBERSHIP, OTHER] } }
const PATH = `/investigations/${INVESTIGATION_ID}`

function organizationOf(call: unknown[] | undefined) {
  return new URL(String(call?.[0]), 'http://x').searchParams.get('organization_id')
}

describe('organization scope', () => {
  it('asks for the selected organization only', async () => {
    const fetchMock = mockFetch({ 'GET /environments': { body: [ENV_ITEM] } })
    renderRoute('/environments')
    expect(await screen.findByText(ENV.name)).toBeInTheDocument()
    expect(organizationOf(requestsTo(fetchMock, 'GET', '/environments')[0])).toBe(ORGANIZATION_ID)
  })

  it('drops the previous organization’s data when switching and asks for the new one', async () => {
    const otherEnv = { ...ENV_ITEM, id: '00000000-0000-4000-8000-0000000000e9', name: 'other-env' }
    const fetchMock = mockFetch({
      'GET /me': TWO_ORGS,
      'GET /environments': ({ url }) => ({
        body: url.searchParams.get('organization_id') === OTHER_ORG ? [otherEnv] : [ENV_ITEM],
      }),
    })
    renderRoute('/environments')
    expect(await screen.findByText(ENV.name)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /switch organization/i }))
    await userEvent.click(await screen.findByRole('menuitem', { name: /Other Org/ }))

    // Same page, now for the other organization.
    expect(await screen.findByText('other-env')).toBeInTheDocument()
    expect(screen.queryByText(ENV.name)).not.toBeInTheDocument()
    const asked = requestsTo(fetchMock, 'GET', '/environments').map(organizationOf)
    expect(asked).toEqual([ORGANIZATION_ID, OTHER_ORG])
  })

  it('shows a record of another organization only after an explicit switch', async () => {
    mockFetch({
      'GET /me': TWO_ORGS,
      [`GET ${PATH}`]: { body: { ...investigation, organization_id: OTHER_ORG } },
      'GET /investigations': { body: { items: [], next_offset: null } },
    })
    renderRoute(PATH)
    expect(await screen.findByText('In another organization')).toBeInTheDocument()
    // Nothing of it is rendered or streamed under the wrong organization.
    expect(screen.queryByText(investigation.title)).not.toBeInTheDocument()
    expect(MockEventSource.instances).toHaveLength(0)

    await userEvent.click(screen.getByRole('button', { name: 'Switch to Other Org' }))
    expect(await screen.findByRole('heading', { name: investigation.title })).toBeInTheDocument()
    expect(MockEventSource.instances).toHaveLength(1)
  })

  it('sends signed-out visitors to the login page and requests no organization data', async () => {
    const fetchMock = mockFetch({
      'GET /auth/session': { body: { ...SIGNED_IN, authenticated: false, user: null } },
    })
    const pages = ['/', '/environments', '/investigations', '/reports', PATH, '/organizations']
    for (const path of [...pages, '/settings']) {
      const { unmount, router } = renderRoute(path)
      expect(await screen.findByRole('heading', { name: 'Welcome back' })).toBeInTheDocument()
      expect(router.state.location.pathname).toBe('/login')
      expect(router.state.location.search).toBe(
        path === '/' ? '' : `?return_to=${encodeURIComponent(path)}`,
      )
      unmount()
    }
    const tenantCalls = fetchMock.mock.calls.filter(
      ([input]) => !/\/api\/(auth\/session|me)$/.test(new URL(String(input), 'http://x').pathname),
    )
    expect(tenantCalls).toEqual([])
  })

  it('explains that data needs an organization', async () => {
    mockFetch({ 'GET /me': { body: { user: TEST_USER, memberships: [] } } })
    renderRoute('/investigations')
    expect(await screen.findByText('No organization yet')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Go to organizations' })).toHaveAttribute(
      'href',
      '/organizations',
    )
  })
})

describe('access ending while signed in', () => {
  it('closes the live stream for good when the server revokes access', async () => {
    mockFetch({
      [`GET ${PATH}`]: [{ body: investigation }, { status: 404, body: { detail: 'not found' } }],
      'GET /investigations': { body: { items: [], next_offset: null } },
    })
    renderRoute(PATH)
    expect(await screen.findByRole('heading', { name: investigation.title })).toBeInTheDocument()
    const source = MockEventSource.latest()
    source.open()

    source.raw('access_revoked', '{"reason": "access_revoked"}')

    expect(source.readyState).toBe(MockEventSource.CLOSED)
    expect(await screen.findByText('Investigation not found')).toBeInTheDocument()
    // No reconnect attempts: the stream cannot succeed any more.
    await new Promise((r) => setTimeout(r, 50))
    expect(MockEventSource.instances).toHaveLength(1)
  })

  it('follows a removed membership: the organization disappears from the switcher', async () => {
    const fetchMock = mockFetch({
      'GET /me': [TWO_ORGS, { body: { user: TEST_USER, memberships: [OTHER] } }],
      'GET /environments': ({ url }) =>
        url.searchParams.get('organization_id') === ORGANIZATION_ID
          ? { status: 404, body: { detail: 'organization not found' } }
          : { body: [] },
    })
    renderRoute('/environments')
    await waitFor(() => expect(requestsTo(fetchMock, 'GET', '/me')).toHaveLength(2))
    expect(await screen.findByText(/no environments yet/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /switch organization/i })).toHaveTextContent(
      'Other Org',
    )
  })

  it('treats a 401 as a signed-out session', async () => {
    mockFetch({
      'GET /auth/session': [{ body: SIGNED_IN }, { body: { ...SIGNED_IN, authenticated: false, user: null, csrf_token: null } }],
      'GET /environments': { status: 401, body: { detail: 'sign in required' } },
    }) // prettier-ignore
    const { router } = renderRoute('/environments')
    // The login page replaces the app, not just an error message on the page.
    expect(await screen.findByRole('heading', { name: 'Welcome back' })).toBeInTheDocument()
    expect(router.state.location.search).toBe('?return_to=%2Fenvironments')
  })
})

function credentialReads(stored: boolean) {
  const status = (kind: string) => ({
    kind, configured: stored, state: stored ? 'configured' : 'not_configured',
    updated_at: stored ? '2026-10-26T10:00:00Z' : null, updated_by: stored ? TEST_USER.id : null,
    main_model: stored && kind === 'nebius' ? 'nvidia/main' : null, local_model: null,
    isolation: stored && kind === 'signoz' ? 'dedicated' : null,
  }) // prettier-ignore
  return {
    [`GET /environments/${ENV.id}/credentials`]: { body: [status('nebius'), status('signoz')] },
    [`GET /organizations/${ORGANIZATION_ID}/members`]: { body: [] },
  }
}

describe('environments of an organization', () => {
  it('creates one in the selected organization, owned by the caller', async () => {
    const created = {
      ...ENV,
      organization_id: ORGANIZATION_ID,
      owner_user_id: TEST_USER.id,
      metadata_json: {},
      created_at: '2026-10-26T10:00:00Z',
      updated_at: '2026-10-26T10:00:00Z',
    }
    const fetchMock = mockFetch({
      'GET /environments': { body: [] },
      'POST /environments': { status: 201, body: created },
      [`GET /environments/${ENV.id}`]: { status: 404, body: { detail: 'x' } },
    })
    const { router } = renderRoute('/environments')
    await userEvent.type(await screen.findByLabelText('Name'), 'checkout-prod')
    await userEvent.type(screen.getByLabelText('Hosts (comma separated)'), 'vm-a, vm-b')
    await userEvent.click(screen.getByRole('button', { name: 'Create environment' }))

    await waitFor(() => expect(router.state.location.pathname).toBe(`/environments/${ENV.id}`))
    const [call] = requestsTo(fetchMock, 'POST', '/environments')
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({
      organization_id: ORGANIZATION_ID,
      name: 'checkout-prod',
      type: 'local',
      metadata_json: { signoz: { hosts: ['vm-a', 'vm-b'], services: [] } },
    })
    expect(new Headers((call?.[1] as RequestInit).headers).get('X-CSRF-Token')).toBe(
      SIGNED_IN.csrf_token,
    )
  })

  it('shows a tenant environment as configuration-required, never with shared settings', async () => {
    const provider = {
      source: 'environment',
      configured: false,
      issues: ['configuration_required'],
    }
    mockFetch({
      [`GET /environments/${ENV.id}`]: {
        body: {
          ...ENV, organization_id: ORGANIZATION_ID, owner_user_id: TEST_USER.id,
          credentials: 'configuration_required', created_at: '2026-10-26T10:00:00Z',
          updated_at: '2026-10-26T10:00:00Z',
          scope: { status: 'missing', hosts: [], services: [], deployment_environment: null, problems: [] },
          stats: { investigations: 0, active_investigations: 0, evidence: 0, tool_calls: 0, last_investigation_at: null },
          telemetry_provider: { provider: 'signoz', ...provider },
          ai_provider: { provider: 'nebius_token_factory', ...provider, main_model: null, local_model: null },
          per_environment_credentials: 'not_available',
        },
      },
      'GET /investigations': { body: { items: [], next_offset: null } },
      ...credentialReads(false),
    }) // prettier-ignore
    renderRoute(`/environments/${ENV.id}?tab=ai`)
    const header = await screen.findByRole('heading', { name: ENV.name })
    expect(
      within(header.parentElement!.parentElement!).getByText('Configuration required'),
    ).toBeInTheDocument()
    expect(
      await screen.findByText(/development settings are never shared with it/),
    ).toBeInTheDocument()
  })

  it.each([
    ['environment', true, [], null],
    ['unavailable', false, ['credentials_unavailable'], 'Credentials unavailable'],
  ] as const)(
    'shows own credentials in state %s without any value',
    async (credentials, configured, issues, badge) => {
      const provider = { source: 'environment', configured, issues: [...issues] }
      mockFetch({
        [`GET /environments/${ENV.id}`]: {
          body: {
            ...ENV, organization_id: ORGANIZATION_ID, owner_user_id: TEST_USER.id,
            credentials, created_at: '2026-10-26T10:00:00Z', updated_at: '2026-10-26T10:00:00Z',
            scope: { status: 'missing', hosts: [], services: [], deployment_environment: null, problems: [] },
            stats: { investigations: 0, active_investigations: 0, evidence: 0, tool_calls: 0, last_investigation_at: null },
            telemetry_provider: { provider: 'signoz', ...provider },
            ai_provider: { provider: 'nebius_token_factory', ...provider,
                           main_model: configured ? 'nvidia/main' : null, local_model: null },
            per_environment_credentials: 'not_available',
          },
        },
        'GET /investigations': { body: { items: [], next_offset: null } },
        ...credentialReads(true),
      }) // prettier-ignore
      renderRoute(`/environments/${ENV.id}?tab=ai`)
      const header = await screen.findByRole('heading', { name: ENV.name })
      const badges = within(header.parentElement!.parentElement!)
      expect(badges.queryByText('Configuration required')).not.toBeInTheDocument()
      if (badge) {
        expect(badges.getByText(badge)).toBeInTheDocument()
        expect(await screen.findByText(/Stored but unreadable/)).toBeInTheDocument()
      } else {
        expect(await screen.findByText('nvidia/main')).toBeInTheDocument()
        expect(screen.queryByText(/Configuration required:/)).not.toBeInTheDocument()
      }
    },
  )
})
