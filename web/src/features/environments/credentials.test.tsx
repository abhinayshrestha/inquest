import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ENV, ORGANIZATION_ID } from '@/test/fixtures'
import { mockFetch, renderRoute, requestsTo, type Reply } from '@/test/render'
import { MEMBERSHIP, SIGNED_IN, TEST_USER } from '@/test/session'

// Phase 7C-4: the environment owner sets, replaces and removes credentials and hands the
// environment on; everyone else sees status only. Secrets live in the open form and in
// the one request body, nowhere else. The server stays authoritative: these tests check the
// page never claims more than it reports and never keeps a secret.

const PATH = `/environments/${ENV.id}`
const SECRET = 'nbk-SENTINEL-frontend-0123456789'
const SIGNOZ_SECRET = 'sgz-SENTINEL-frontend-abcdef'
const OMAR = '00000000-0000-4000-8000-0000000000a2'
const ADA = '00000000-0000-4000-8000-0000000000a3'
const TRANSFER = '00000000-0000-4000-8000-0000000000c1'

const MEMBERS = [
  { user_id: TEST_USER.id, email: TEST_USER.email, display_name: 'Tess', role: 'owner', joined_at: '2026-10-26T10:00:00Z' },
  { user_id: OMAR, email: 'omar@example.com', display_name: 'Omar', role: 'member', joined_at: '2026-10-26T10:00:00Z' },
  { user_id: ADA, email: 'ada@example.com', display_name: 'Ada', role: 'admin', joined_at: '2026-10-26T10:00:00Z' },
] // prettier-ignore

type State = 'environment' | 'process_development' | 'configuration_required' | 'unavailable'

function detail({ owner = TEST_USER.id, credentials = 'configuration_required' as State, issues = [] as string[] } = {}) {
  const ready = credentials === 'environment'
  const provider = { source: 'environment', configured: ready, issues: ready ? [] : issues.length ? issues : ['configuration_required'] }
  return {
    ...ENV, organization_id: ORGANIZATION_ID, owner_user_id: owner, credentials,
    created_at: '2026-10-26T10:00:00Z', updated_at: '2026-10-26T10:00:00Z',
    scope: { status: 'configured', hosts: ['vm'], services: [], deployment_environment: null, problems: [] },
    stats: { investigations: 1, active_investigations: 1, evidence: 0, tool_calls: 0, last_investigation_at: null },
    telemetry_provider: { provider: 'signoz', ...provider },
    ai_provider: { provider: 'nebius_token_factory', ...provider, main_model: ready ? 'nvidia/main' : null, local_model: ready ? 'nvidia/local' : null },
    per_environment_credentials: 'not_available',
  }
} // prettier-ignore

function status(kind: 'nebius' | 'signoz', state: 'configured' | 'not_configured' | 'unavailable') {
  const stored = state !== 'not_configured'
  return {
    kind,
    configured: state === 'configured',
    state,
    updated_at: stored ? '2026-10-26T12:00:00Z' : null,
    updated_by: stored ? TEST_USER.id : null,
    main_model: kind === 'nebius' && stored ? 'nvidia/main' : null,
    local_model: kind === 'nebius' && stored ? 'nvidia/local' : null,
    isolation: kind === 'signoz' && stored ? ('dedicated' as const) : null,
  }
}

const NONE = [status('nebius', 'not_configured'), status('signoz', 'not_configured')]
const BOTH = [status('nebius', 'configured'), status('signoz', 'configured')]

function backend({
  env = { body: detail() } as Reply | Reply[],
  credentials = { body: NONE } as Reply | Reply[],
  extra = {} as Record<string, Reply | Reply[] | ((r: { url: URL; body: unknown }) => Reply)>,
} = {}) {
  return mockFetch({
    [`GET ${PATH}`]: env,
    [`GET ${PATH}/credentials`]: credentials,
    [`GET ${PATH}/telemetry`]: { body: { signals: [], queries_checked: 0, queries_failed: 0 } },
    [`GET /organizations/${ORGANIZATION_ID}/members`]: { body: MEMBERS },
    'GET /investigations': { body: { items: [], next_offset: null } },
    ...extra,
  })
}

async function card(title: string) {
  return waitFor(() => {
    const found = [...document.querySelectorAll('[data-slot=card-title]')].find((e) =>
      e.textContent?.startsWith(title),
    )
    if (!found) throw new Error(`no card ${title}`)
    return found.closest<HTMLElement>('[data-slot=card]')!
  })
}

let logged: unknown[][]
beforeEach(() => {
  logged = []
  for (const level of ['log', 'info', 'warn', 'error', 'debug'] as const)
    vi.spyOn(console, level).mockImplementation((...args: unknown[]) => void logged.push(args))
})
afterEach(() => vi.restoreAllMocks())

// Every place a secret could linger after the form is done with it.
function assertNoSecret(
  secret: string,
  { queryClient, router }: ReturnType<typeof renderRoute>,
  fetchMock?: ReturnType<typeof mockFetch>,
) {
  const places = {
    queries: JSON.stringify(
      queryClient
        .getQueryCache()
        .getAll()
        .map((q) => [q.queryKey, q.state]),
    ),
    mutations: JSON.stringify(
      queryClient
        .getMutationCache()
        .getAll()
        .map((m) => [m.options.mutationKey, m.state]),
    ),
    localStorage: JSON.stringify({ ...window.localStorage }),
    sessionStorage: JSON.stringify({ ...window.sessionStorage }),
    url: router.state.location.pathname + router.state.location.search + router.state.location.hash,
    page: document.body.textContent ?? '',
    inputs: [...document.querySelectorAll('input')].map((i) => i.value).join(' '),
    logs: JSON.stringify(logged),
    // Only the PUT body may carry it; never a URL or header.
    requests: JSON.stringify(
      (fetchMock?.mock.calls ?? []).map(([input, init]) => [String(input), init?.headers ?? null]),
    ),
  }
  for (const [place, text] of Object.entries(places)) expect(text, place).not.toContain(secret)
}

async function openNebiusForm() {
  await userEvent.click(await screen.findByRole('button', { name: /set up credentials/i }))
  return screen.getByRole('form', { name: 'Set Nebius credentials' })
}

describe('credential status', () => {
  it('shows Nebius and SigNoz separately from the backend status, without values', async () => {
    backend({
      env: { body: detail({ credentials: 'configuration_required' }) },
      credentials: { body: [status('nebius', 'configured'), status('signoz', 'not_configured')] },
    })
    const { unmount } = renderRoute(`${PATH}?tab=ai`)
    const nebius = await card('Nebius credentials')
    expect((await within(nebius).findAllByText('Configured')).length).toBeGreaterThan(0)
    expect(within(nebius).getByText('nvidia/main')).toBeInTheDocument()
    expect(screen.getByText(/Configuration required: investigations need both/)).toBeInTheDocument()
    expect(screen.getByText('Never shown or sent to the browser')).toBeInTheDocument()
    unmount()

    renderRoute(`${PATH}?tab=telemetry`)
    const signoz = await card('SigNoz credentials')
    expect((await within(signoz).findAllByText('Missing')).length).toBeGreaterThan(0)
    // Configuration status only: no invented reachability.
    expect(screen.getByText(/not that\s+SigNoz is reachable right now/)).toBeInTheDocument()
  })

  it.each([
    [['credentials_unavailable'], /Stored but unreadable/],
    [['destination_not_allowed'], /Stored endpoint not allowed/],
  ])('explains stored credentials the server cannot use (%s)', async (issues, message) => {
    backend({
      env: { body: detail({ credentials: 'unavailable', issues }) },
      credentials: { body: [status('nebius', 'unavailable'), status('signoz', 'configured')] },
    })
    renderRoute(`${PATH}?tab=ai`)
    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(screen.getAllByText('Unavailable').length).toBeGreaterThan(0)
  })
})

describe('who may change credentials', () => {
  it.each([
    ['a member', 'member'],
    ['an organization admin', 'admin'],
    ['an organization owner', 'owner'],
  ])('offers no credential form to %s who does not own the environment', async (_, role) => {
    const fetchMock = backend({
      env: { body: detail({ owner: OMAR, credentials: 'environment' }) },
      credentials: { body: BOTH },
      extra: { 'GET /me': { body: { user: TEST_USER, memberships: [{ ...MEMBERSHIP, role }] } } },
    })
    renderRoute(`${PATH}?tab=ai`)
    expect(await screen.findByText(/Only Omar, this environment’s owner/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /credentials/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /remove/i })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('API key')).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.every(([, init]) => (init?.method ?? 'GET') === 'GET')).toBe(true)
  })
})

describe('setting and replacing', () => {
  it('saves new Nebius credentials, then forgets the key everywhere', async () => {
    const fetchMock = backend({
      env: [{ body: detail() }, { body: detail({ credentials: 'environment' }) }],
      credentials: [{ body: NONE }, { body: [status('nebius', 'configured'), NONE[1]] }],
      extra: { [`PUT ${PATH}/credentials/nebius`]: { body: status('nebius', 'configured') } },
    })
    const view = renderRoute(`${PATH}?tab=ai`)
    const form = await openNebiusForm()
    const key = within(form).getByLabelText('API key')
    expect(key).toHaveAttribute('type', 'password')
    expect(key).toHaveValue('') // never prefilled
    await userEvent.type(key, SECRET)
    await userEvent.type(within(form).getByLabelText('Main Investigator model'), 'nvidia/main')
    await userEvent.type(within(form).getByLabelText('Local Investigator model'), 'nvidia/local')
    await userEvent.click(within(form).getByRole('button', { name: /save credentials/i }))

    expect(await screen.findByRole('status')).toHaveTextContent('Nebius credentials saved.')
    expect(screen.queryByRole('form')).not.toBeInTheDocument()
    const [put] = requestsTo(fetchMock, 'PUT', `${PATH}/credentials/nebius`)
    const init = put![1] as RequestInit
    expect(JSON.parse(String(init.body))).toEqual({
      api_key: SECRET,
      base_url: 'https://api.tokenfactory.nebius.com/v1/',
      main_model: 'nvidia/main',
      local_model: 'nvidia/local',
    })
    expect(new Headers(init.headers).get('X-CSRF-Token')).toBe(SIGNED_IN.csrf_token)
    // Status, readiness and the investigations a change can affect are re-read.
    await waitFor(() =>
      expect(requestsTo(fetchMock, 'GET', `${PATH}/credentials`).length).toBeGreaterThan(1),
    )
    expect(requestsTo(fetchMock, 'GET', PATH).length).toBeGreaterThan(1)
    expect(
      await screen.findByText(/Ready: investigations of this environment run/),
    ).toBeInTheDocument()
    assertNoSecret(SECRET, view, fetchMock)
  })

  it('replaces a key without prefilling it and says when it takes effect', async () => {
    const fetchMock = backend({
      env: { body: detail({ credentials: 'environment' }) },
      credentials: { body: BOTH },
      extra: { [`PUT ${PATH}/credentials/nebius`]: { body: status('nebius', 'configured') } },
    })
    const view = renderRoute(`${PATH}?tab=ai`)
    await userEvent.click(await screen.findByRole('button', { name: /replace credentials/i }))
    const form = screen.getByRole('form', { name: 'Replace Nebius credentials' })
    expect(within(form).getByLabelText('API key')).toHaveValue('')
    // Model ids are public configuration; reusing them is a convenience.
    expect(within(form).getByLabelText('Main Investigator model')).toHaveValue('nvidia/main')
    expect(within(form).getByText(/next model step of running investigations/)).toBeInTheDocument()
    await userEvent.type(within(form).getByLabelText('API key'), SECRET)
    await userEvent.click(within(form).getByRole('button', { name: /save credentials/i }))
    expect(await screen.findByRole('status')).toHaveTextContent(/replaced.*next model step/)
    expect(requestsTo(fetchMock, 'PUT', `${PATH}/credentials/nebius`)).toHaveLength(1)
    assertNoSecret(SECRET, view, fetchMock)
  })

  it('checks the form before sending anything', async () => {
    const fetchMock = backend()
    renderRoute(`${PATH}?tab=ai`)
    const form = await openNebiusForm()
    await userEvent.type(within(form).getByLabelText('API key'), 'short')
    await userEvent.clear(within(form).getByLabelText('Provider URL'))
    await userEvent.type(within(form).getByLabelText('Provider URL'), 'http://nebius.example')
    await userEvent.click(within(form).getByRole('button', { name: /save credentials/i }))
    const key = within(form).getByLabelText('API key')
    expect(key).toHaveAttribute('aria-invalid', 'true')
    expect(key).toHaveAccessibleDescription(/at least 20 characters/)
    expect(within(form).getByLabelText('Provider URL')).toHaveAccessibleDescription(/https:\/\//)
    expect(within(form).queryByText(/short/)).not.toBeInTheDocument()
    expect(requestsTo(fetchMock, 'PUT', `${PATH}/credentials/nebius`)).toHaveLength(0)
  })

  it('explains a provider refusal in its own words, once, without the key', async () => {
    const fetchMock = backend({
      extra: {
        [`PUT ${PATH}/credentials/nebius`]: {
          status: 422,
          body: {
            detail: { code: 'auth_failed', message: 'the provider rejected these credentials' },
          },
        },
      },
    })
    const view = renderRoute(`${PATH}?tab=ai`)
    const form = await openNebiusForm()
    await userEvent.type(within(form).getByLabelText('API key'), SECRET)
    await userEvent.type(within(form).getByLabelText('Main Investigator model'), 'm')
    await userEvent.type(within(form).getByLabelText('Local Investigator model'), 'l')
    await userEvent.click(within(form).getByRole('button', { name: /save credentials/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The provider rejected this API key. Nothing was saved.',
    )
    // Not retried; the form stays open to fix it.
    expect(requestsTo(fetchMock, 'PUT', `${PATH}/credentials/nebius`)).toHaveLength(1)
    expect(screen.getByRole('form')).toBeInTheDocument()
    await userEvent.click(within(form).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('form')).not.toBeInTheDocument()
    assertNoSecret(SECRET, view, fetchMock)
  })

  it('explains that private SigNoz endpoints need the server allowlist', async () => {
    const fetchMock = backend({
      extra: {
        [`PUT ${PATH}/credentials/signoz`]: {
          status: 422,
          body: { detail: { code: 'destination_not_allowed', message: 'not allowed' } },
        },
      },
    })
    const view = renderRoute(`${PATH}?tab=telemetry`)
    await userEvent.click(await screen.findByRole('button', { name: /set up credentials/i }))
    const form = screen.getByRole('form', { name: 'Set SigNoz credentials' })
    expect(within(form).getByLabelText('SigNoz URL')).toHaveAccessibleDescription(
      /allowlisted that exact origin/,
    )
    await userEvent.type(within(form).getByLabelText('SigNoz URL'), 'http://localhost:8080')
    await userEvent.type(within(form).getByLabelText('API key (Viewer role)'), SIGNOZ_SECRET)
    await userEvent.click(within(form).getByRole('button', { name: /save credentials/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /local, private and plain-http endpoints work only that way/,
    )
    const [put] = requestsTo(fetchMock, 'PUT', `${PATH}/credentials/signoz`)
    expect(JSON.parse(String((put![1] as RequestInit).body))).toEqual({
      base_url: 'http://localhost:8080',
      api_key: SIGNOZ_SECRET,
      isolation: 'dedicated',
    })
    view.unmount()
    // Unmounting drops the open form's values with it.
    expect(document.body.textContent).not.toContain(SIGNOZ_SECRET)
    assertNoSecret(SIGNOZ_SECRET, view)
  })
})

describe('telemetry isolation (7D-2B)', () => {
  it('sends the chosen isolation and explains a key that is not confined', async () => {
    const fetchMock = backend({
      extra: {
        [`PUT ${PATH}/credentials/signoz`]: {
          status: 422,
          body: { detail: { code: 'scope_not_enforced', message: 'not confined' } },
        },
      },
    })
    const view = renderRoute(`${PATH}?tab=telemetry`)
    await userEvent.click(await screen.findByRole('button', { name: /set up credentials/i }))
    const form = screen.getByRole('form', { name: 'Set SigNoz credentials' })
    const isolation = within(form).getByLabelText('Isolation from other organizations')
    expect(isolation).toHaveValue('dedicated')
    await userEvent.selectOptions(isolation, 'provider_scoped')
    expect(isolation).toHaveAccessibleDescription(/operator assigns your ingestion keys/)
    await userEvent.type(within(form).getByLabelText('SigNoz URL'), 'https://acme.signoz.cloud')
    await userEvent.type(within(form).getByLabelText('API key (Viewer role)'), SIGNOZ_SECRET)
    await userEvent.click(within(form).getByRole('button', { name: /save credentials/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /can read telemetry beyond your organization’s ingestion keys/,
    )
    const [put] = requestsTo(fetchMock, 'PUT', `${PATH}/credentials/signoz`)
    expect(JSON.parse(String((put![1] as RequestInit).body))).toEqual({
      base_url: 'https://acme.signoz.cloud',
      api_key: SIGNOZ_SECRET,
      isolation: 'provider_scoped',
    })
    view.unmount()
    assertNoSecret(SIGNOZ_SECRET, view)
  })

  it('shows how a stored connection is isolated and why it stopped', async () => {
    const scoped = { ...status('signoz', 'configured'), isolation: 'provider_scoped' as const }
    backend({
      env: {
        body: detail({ credentials: 'unavailable', issues: ['telemetry_isolation_unverified'] }),
      },
      credentials: { body: [status('nebius', 'configured'), scoped] },
    })
    renderRoute(`${PATH}?tab=telemetry`)
    const section = await card('SigNoz credentials')
    expect(
      await within(section).findByText(/restricted to this organization’s ingestion keys/),
    ).toBeInTheDocument()
    expect(screen.getByText(/Telemetry not verified as isolated/)).toBeInTheDocument()
  })
})

describe('removing', () => {
  it('confirms, warns about running investigations, deletes once and refreshes', async () => {
    const fetchMock = backend({
      env: [{ body: detail({ credentials: 'environment' }) }, { body: detail() }],
      credentials: [{ body: BOTH }, { body: [NONE[0], BOTH[1]] }],
      extra: { [`DELETE ${PATH}/credentials/nebius`]: { status: 204 } },
    })
    const { queryClient } = renderRoute(`${PATH}?tab=ai`)
    // An investigation list seen earlier: a removal cancels runs, so it must be re-read.
    queryClient.setQueryData(['investigations', ORGANIZATION_ID], { items: [], next_offset: null })
    await userEvent.click(await screen.findByRole('button', { name: 'Remove' }))
    const dialog = screen.getByRole('alertdialog', { name: 'Remove the Nebius credential?' })
    expect(dialog).toHaveTextContent(/running now are cancelled/)
    expect(dialog).toHaveTextContent(/Past investigations, evidence and reports stay/)
    // Focus moves into the dialog; keeping it sends nothing.
    expect(dialog.contains(document.activeElement)).toBe(true)
    await userEvent.click(within(dialog).getByRole('button', { name: 'Keep it' }))
    expect(requestsTo(fetchMock, 'DELETE', `${PATH}/credentials/nebius`)).toHaveLength(0)

    await userEvent.click(screen.getByRole('button', { name: 'Remove' }))
    await userEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: /remove and cancel/i }),
    )
    expect(await screen.findByRole('status')).toHaveTextContent(/removed.*were cancelled/)
    expect(requestsTo(fetchMock, 'DELETE', `${PATH}/credentials/nebius`)).toHaveLength(1)
    await waitFor(() =>
      expect(queryClient.getQueryState(['investigations', ORGANIZATION_ID])?.isInvalidated).toBe(
        true,
      ),
    )
    expect(await screen.findByRole('button', { name: /set up credentials/i })).toBeInTheDocument()
  })

  it('reports a failed removal once, without retrying', async () => {
    const fetchMock = backend({
      env: { body: detail({ credentials: 'environment' }) },
      credentials: { body: BOTH },
      extra: { [`DELETE ${PATH}/credentials/nebius`]: { status: 500, body: { detail: 'boom' } } },
    })
    renderRoute(`${PATH}?tab=ai`)
    await userEvent.click(await screen.findByRole('button', { name: 'Remove' }))
    await userEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: /remove and cancel/i }),
    )
    expect(await screen.findByRole('alert')).toHaveTextContent('The request failed.')
    expect(requestsTo(fetchMock, 'DELETE', `${PATH}/credentials/nebius`)).toHaveLength(1)
  })
})

describe('access changing while the form is open', () => {
  async function fillAndSave() {
    const form = await openNebiusForm()
    await userEvent.type(within(form).getByLabelText('API key'), SECRET)
    await userEvent.type(within(form).getByLabelText('Main Investigator model'), 'm')
    await userEvent.type(within(form).getByLabelText('Local Investigator model'), 'l')
    await userEvent.click(within(form).getByRole('button', { name: /save credentials/i }))
  }

  it('closes the form when ownership moved to someone else', async () => {
    const fetchMock = backend({
      env: [{ body: detail() }, { body: detail({ owner: OMAR }) }],
      extra: {
        [`PUT ${PATH}/credentials/nebius`]: {
          status: 403,
          body: { detail: 'only the environment owner can change its credentials' },
        },
      },
    })
    const view = renderRoute(`${PATH}?tab=ai`)
    await fillAndSave()
    expect(await screen.findByText(/Only Omar, this environment’s owner/)).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(/Ownership may have changed/)
    expect(screen.queryByRole('form')).not.toBeInTheDocument()
    assertNoSecret(SECRET, view, fetchMock)
  })

  it('leaves the page when membership ended', async () => {
    const fetchMock = backend({
      env: [{ body: detail() }, { status: 404, body: { detail: 'environment not found' } }],
      extra: {
        [`PUT ${PATH}/credentials/nebius`]: {
          status: 404,
          body: { detail: 'environment not found' },
        },
      },
    })
    const view = renderRoute(`${PATH}?tab=ai`)
    await fillAndSave()
    expect(await screen.findByText('Environment not found')).toBeInTheDocument()
    assertNoSecret(SECRET, view, fetchMock)
  })

  it('asks to sign in again when the session expired', async () => {
    const fetchMock = backend({
      extra: {
        'GET /auth/session': [
          { body: SIGNED_IN },
          { body: { ...SIGNED_IN, authenticated: false, user: null, csrf_token: null } },
        ],
        [`PUT ${PATH}/credentials/nebius`]: { status: 401, body: { detail: 'sign in required' } },
      },
    })
    const view = renderRoute(`${PATH}?tab=ai`)
    await fillAndSave()
    expect(await screen.findByText('Sign in required')).toBeInTheDocument()
    expect(screen.queryByRole('form')).not.toBeInTheDocument()
    assertNoSecret(SECRET, view, fetchMock)
  })

  it('refreshes a stale CSRF token and keeps the form for another try', async () => {
    const fetchMock = backend({
      extra: {
        [`PUT ${PATH}/credentials/nebius`]: { status: 403, body: { detail: 'invalid CSRF token' } },
      },
    })
    renderRoute(`${PATH}?tab=ai`)
    const before = requestsTo(fetchMock, 'GET', '/auth/session').length
    await fillAndSave()
    expect(await screen.findByRole('alert')).toHaveTextContent(/security token was out of date/)
    await waitFor(() =>
      expect(requestsTo(fetchMock, 'GET', '/auth/session').length).toBeGreaterThan(before),
    )
    expect(screen.getByRole('form')).toBeInTheDocument()
    expect(requestsTo(fetchMock, 'PUT', `${PATH}/credentials/nebius`)).toHaveLength(1)
  })
})

describe('ownership transfers', () => {
  const transfer = (direction: 'incoming' | 'outgoing', status = 'pending') => ({
    id: TRANSFER,
    environment_id: ENV.id,
    environment_name: ENV.name,
    organization_id: ORGANIZATION_ID,
    from_user_id: direction === 'incoming' ? OMAR : TEST_USER.id,
    to_user_id: direction === 'incoming' ? TEST_USER.id : OMAR,
    status,
    direction,
    created_at: '2026-10-27T09:00:00Z',
    resolved_at: status === 'pending' ? null : '2026-10-27T10:00:00Z',
  })

  it('lets the owner offer it to another member, then withdraw the offer', async () => {
    let pending = false
    const fetchMock = backend({
      env: { body: detail({ credentials: 'environment' }) },
      credentials: { body: BOTH },
      extra: {
        'GET /transfers': () => ({ body: pending ? [transfer('outgoing')] : [] }),
        [`POST ${PATH}/transfers`]: () => {
          pending = true
          return { status: 201, body: transfer('outgoing') }
        },
        [`POST /transfers/${TRANSFER}/cancel`]: () => {
          pending = false
          return { body: transfer('outgoing', 'cancelled') }
        },
      },
    })
    renderRoute(`${PATH}?tab=settings`)
    const select = await screen.findByLabelText('New owner')
    // Other members only, admins included; never the owner.
    expect(
      within(select)
        .getAllByRole('option')
        .map((o) => o.textContent),
    ).toEqual(['Choose a member…', 'Omar', 'Ada'])
    await userEvent.selectOptions(select, OMAR)
    await userEvent.click(screen.getByRole('button', { name: /offer ownership/i }))
    const dialog = screen.getByRole('alertdialog', { name: 'Offer this environment to Omar?' })
    expect(dialog).toHaveTextContent(
      /credentials for this environment are deleted, never handed over/,
    )
    expect(dialog).toHaveTextContent(/Past investigations, evidence and reports stay/)
    await userEvent.click(within(dialog).getByRole('button', { name: 'Send offer' }))

    expect(await screen.findByText(/Offered to/)).toHaveTextContent('Omar')
    const [post] = requestsTo(fetchMock, 'POST', `${PATH}/transfers`)
    expect(JSON.parse(String((post![1] as RequestInit).body))).toEqual({ to_user_id: OMAR })

    await userEvent.click(screen.getByRole('button', { name: /withdraw offer/i }))
    expect(await screen.findByLabelText('New owner')).toBeInTheDocument()
    expect(requestsTo(fetchMock, 'POST', `/transfers/${TRANSFER}/cancel`)).toHaveLength(1)
  })

  it('shows an incoming offer in the header and lets the recipient accept it', async () => {
    let accepted = false
    const fetchMock = mockFetch({
      [`GET ${PATH}`]: () => ({
        body: accepted ? detail() : detail({ owner: OMAR, credentials: 'environment' }),
      }),
      [`GET ${PATH}/credentials`]: () => ({ body: accepted ? NONE : BOTH }),
      [`GET ${PATH}/telemetry`]: { body: { signals: [], queries_checked: 0, queries_failed: 0 } },
      [`GET /organizations/${ORGANIZATION_ID}/members`]: { body: MEMBERS },
      'GET /investigations': { body: { items: [], next_offset: null } },
      'GET /environments': { body: [] },
      'GET /transfers': () => ({ body: accepted ? [] : [transfer('incoming')] }),
      [`POST /transfers/${TRANSFER}/accept`]: () => {
        accepted = true
        return { body: transfer('incoming', 'accepted') }
      },
    })
    const { router } = renderRoute('/environments')
    await userEvent.click(await screen.findByRole('button', { name: '1 ownership offer waiting' }))
    await userEvent.click(await screen.findByRole('menuitem', { name: new RegExp(ENV.name) }))
    await waitFor(() => expect(router.state.location.search).toBe('?tab=settings'))

    const offer = await screen.findByRole('region', { name: 'Ownership offer' })
    expect(offer).toHaveTextContent('Omar offered you ownership')
    await userEvent.click(within(offer).getByRole('button', { name: 'Accept ownership' }))
    const dialog = screen.getByRole('alertdialog', {
      name: 'Become the owner of this environment?',
    })
    expect(dialog).toHaveTextContent(
      /Nebius and SigNoz credentials for this environment are deleted/,
    )
    expect(dialog).toHaveTextContent(/Past investigations, evidence and reports stay/)
    await userEvent.click(within(dialog).getByRole('button', { name: 'Accept ownership' }))

    // Now the owner, and the environment needs the new owner's own credentials.
    expect(
      await screen.findByText(/You own this environment but it has no credentials/),
    ).toBeInTheDocument()
    expect(screen.getAllByText('Configuration required').length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: /ownership offer/ })).not.toBeInTheDocument()
    expect(requestsTo(fetchMock, 'POST', `/transfers/${TRANSFER}/accept`)).toHaveLength(1)
    await userEvent.click(screen.getByRole('tab', { name: 'AI provider' }))
    expect(await screen.findByRole('button', { name: /set up credentials/i })).toBeInTheDocument()
  })

  it('lets the recipient decline', async () => {
    let declined = false
    const fetchMock = backend({
      env: { body: detail({ owner: OMAR, credentials: 'environment' }) },
      credentials: { body: BOTH },
      extra: {
        'GET /transfers': () => ({ body: declined ? [] : [transfer('incoming')] }),
        [`POST /transfers/${TRANSFER}/decline`]: () => {
          declined = true
          return { body: transfer('incoming', 'declined') }
        },
      },
    })
    renderRoute(`${PATH}?tab=settings`)
    const offer = await screen.findByRole('region', { name: 'Ownership offer' })
    await userEvent.click(within(offer).getByRole('button', { name: 'Decline' }))
    await waitFor(() =>
      expect(screen.queryByRole('region', { name: 'Ownership offer' })).not.toBeInTheDocument(),
    )
    expect(screen.getByText('Only the owner can transfer this environment.')).toBeInTheDocument()
    expect(requestsTo(fetchMock, 'POST', `/transfers/${TRANSFER}/decline`)).toHaveLength(1)
  })

  it('explains a refused transfer action with the server’s message', async () => {
    backend({
      env: { body: detail({ owner: OMAR, credentials: 'environment' }) },
      credentials: { body: BOTH },
      extra: {
        'GET /transfers': { body: [transfer('incoming')] },
        [`POST /transfers/${TRANSFER}/decline`]: {
          status: 409,
          body: { detail: 'this transfer is no longer pending' },
        },
      },
    })
    renderRoute(`${PATH}?tab=settings`)
    const offer = await screen.findByRole('region', { name: 'Ownership offer' })
    await userEvent.click(within(offer).getByRole('button', { name: 'Decline' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('this transfer is no longer pending')
  })
})
