import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ACTIVITY_INTERVAL_MS, pageNavigation } from '@/app/contexts/auth'
import { setCsrfToken } from '@/lib/api/client'
import { inviteInput, PERMISSIONS } from '@/lib/api/organizations'
import { mockFetch, renderRoute, requestsTo } from '@/test/render'
import { SIGNED_OUT_UNCONFIGURED } from '@/test/session'

const USER = {
  id: '00000000-0000-4000-8000-0000000000a1',
  email: 'ada@example.com',
  email_verified: true,
  display_name: 'Ada',
}
const OTHER = '00000000-0000-4000-8000-0000000000b2'
const ORG = '00000000-0000-4000-8000-0000000000c3'
const ORG_2 = '00000000-0000-4000-8000-0000000000c4'
const INVITE = '00000000-0000-4000-8000-0000000000d5'

const SIGNED_IN = {
  configured: true,
  authenticated: true,
  user: USER,
  csrf_token: 'csrf-from-session',
  expires_at: '2026-10-27T00:00:00Z',
}
const SIGNED_OUT = {
  ...SIGNED_IN,
  authenticated: false,
  user: null,
  csrf_token: null,
  expires_at: null,
}

const membership = (id: string, name: string, role = 'owner') => ({
  organization_id: id,
  name,
  slug: name.toLowerCase().replace(/ /g, '-'),
  role,
})
const organization = (role = 'owner') => ({
  id: ORG,
  name: 'Platform',
  slug: 'platform',
  created_at: '2026-10-20T00:00:00Z',
  role,
  member_count: 2,
})
const members = [
  { user_id: USER.id, email: USER.email, display_name: 'Ada', role: 'owner', joined_at: '2026-10-20T00:00:00Z' },
  { user_id: OTHER, email: 'bob@example.com', display_name: 'Bob', role: 'member', joined_at: '2026-10-21T00:00:00Z' },
] // prettier-ignore
const pending = {
  id: INVITE,
  email: 'cy@example.com',
  role: 'member',
  status: 'pending',
  invited_by: 'Ada',
  created_at: '2026-10-25T00:00:00Z',
  expires_at: '2026-11-01T00:00:00Z',
}

function headerOf(call: unknown[] | undefined, name: string) {
  const init = call?.[1] as RequestInit | undefined
  return new Headers(init?.headers).get(name)
}

afterEach(() => {
  setCsrfToken(null)
  window.sessionStorage.clear()
  vi.restoreAllMocks()
})

describe('sign-in states', () => {
  it('fails closed when sign-in is not configured: no organization requests, no fallback', async () => {
    const fetchMock = mockFetch({ 'GET /auth/session': { body: SIGNED_OUT_UNCONFIGURED } })
    renderRoute('/organizations')
    expect(await screen.findByText('Sign-in is not configured')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /sign-on|sign in/i })).not.toBeInTheDocument()
    expect(requestsTo(fetchMock, 'GET', '/me')).toHaveLength(0)
  })

  it('sends signed-out visitors to the login page, then to the backend login', async () => {
    mockFetch({ 'GET /auth/session': { body: SIGNED_OUT } })
    const assign = vi.spyOn(pageNavigation, 'assign').mockImplementation(() => {})
    const { router } = renderRoute('/organizations?tab=members')
    expect(await screen.findByRole('heading', { name: 'Welcome back' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
    await userEvent.click(screen.getByRole('button', { name: /continue with single sign-on/i }))
    expect(assign).toHaveBeenCalledWith(
      '/api/auth/login?return_to=%2Forganizations%3Ftab%3Dmembers',
    )
  })

  it('never returns to another site after signing in', async () => {
    mockFetch({ 'GET /auth/session': { body: SIGNED_OUT } })
    const assign = vi.spyOn(pageNavigation, 'assign').mockImplementation(() => {})
    for (const target of ['//evil.example', 'https://evil.example', '/\\evil.example', '/login']) {
      const { unmount } = renderRoute(`/login?return_to=${encodeURIComponent(target)}`)
      await userEvent.click(
        await screen.findByRole('button', { name: /continue with single sign-on/i }),
      )
      expect(assign).toHaveBeenLastCalledWith('/api/auth/login?return_to=%2F')
      unmount()
    }
  })

  it('takes a signed-in visitor from the login page to where they were going', async () => {
    mockFetch({
      'GET /auth/session': { body: SIGNED_IN },
      'GET /me': { body: { user: USER, memberships: [] } },
    })
    const { router } = renderRoute('/login?return_to=%2Forganizations')
    expect(await screen.findByRole('heading', { level: 1, name: 'Organizations' }))
    expect(router.state.location.pathname).toBe('/organizations')
  })

  it('shows the signed-in user and signs out with the CSRF token', async () => {
    const fetchMock = mockFetch({
      'GET /auth/session': [{ body: SIGNED_IN }, { body: SIGNED_OUT }],
      'GET /me': { body: { user: USER, memberships: [] } },
      'POST /auth/logout': { status: 204 },
    })
    const { router } = renderRoute('/organizations')
    await userEvent.click(await screen.findByRole('button', { name: 'Account: Ada' }))
    await userEvent.click(await screen.findByRole('menuitem', { name: 'Sign out' }))
    await waitFor(() => expect(requestsTo(fetchMock, 'POST', '/auth/logout')).toHaveLength(1))
    expect(headerOf(requestsTo(fetchMock, 'POST', '/auth/logout')[0], 'X-CSRF-Token')).toBe(
      'csrf-from-session',
    )
    // A deliberate sign-out lands on the login page without a way back into the old page.
    expect(await screen.findByText('You have signed out')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
    expect(router.state.location.search).toBe('?signed_out=signed_out')
  })

  it('signs out everywhere after confirming, with the CSRF token, and says so', async () => {
    const fetchMock = mockFetch({
      'GET /auth/session': [{ body: SIGNED_IN }, { body: SIGNED_OUT }],
      'GET /me': { body: { user: USER, memberships: [] } },
      'POST /auth/logout-everywhere': { status: 204 },
    })
    renderRoute('/organizations')
    await userEvent.click(await screen.findByRole('button', { name: 'Account: Ada' }))
    await userEvent.click(await screen.findByRole('menuitem', { name: 'Sign out everywhere' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText(/organizations, investigations and reports are kept/))
    expect(requestsTo(fetchMock, 'POST', '/auth/logout-everywhere')).toHaveLength(0)
    await userEvent.click(within(dialog).getByRole('button', { name: 'Sign out everywhere' }))
    expect(await screen.findByText(/ended, on every device/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Welcome back' })).toBeInTheDocument()
    const [call] = requestsTo(fetchMock, 'POST', '/auth/logout-everywhere')
    expect(headerOf(call, 'X-CSRF-Token')).toBe('csrf-from-session')
    expect(requestsTo(fetchMock, 'POST', '/auth/logout')).toHaveLength(0)
  })

  it.each([
    [500, 'Could not sign out everywhere. Your sessions are unchanged; try again.'],
    [429, 'Too many attempts. Your sessions are unchanged; try again later.'],
  ])('keeps the session and explains a failed sign out everywhere (%i)', async (status, text) => {
    mockFetch({
      'GET /auth/session': { body: SIGNED_IN },
      'GET /me': { body: { user: USER, memberships: [] } },
      'POST /auth/logout-everywhere': { status, body: { detail: '<b>server text</b>' } },
    })
    renderRoute('/organizations')
    await userEvent.click(await screen.findByRole('button', { name: 'Account: Ada' }))
    await userEvent.click(await screen.findByRole('menuitem', { name: 'Sign out everywhere' }))
    const dialog = await screen.findByRole('alertdialog')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Sign out everywhere' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(text)
    expect(screen.queryByText(/server text/)).not.toBeInTheDocument()
    await userEvent.click(within(dialog).getByRole('button', { name: 'Go back' }))
    expect(screen.getByRole('button', { name: 'Account: Ada' })).toBeInTheDocument()
  })

  it('reports real input as activity at most once a minute, never on its own', async () => {
    const fetchMock = mockFetch({
      'GET /auth/session': { body: SIGNED_IN },
      'GET /me': { body: { user: USER, memberships: [] } },
      'POST /auth/activity': { status: 204 },
    })
    let now = Date.now()
    vi.spyOn(Date, 'now').mockImplementation(() => now)
    renderRoute('/organizations')
    await screen.findByRole('button', { name: 'Account: Ada' })
    fireEvent.keyDown(window)
    now += ACTIVITY_INTERVAL_MS + 1
    expect(requestsTo(fetchMock, 'POST', '/auth/activity')).toHaveLength(0)
    fireEvent.pointerDown(window)
    fireEvent.keyDown(window)
    fireEvent.wheel(window)
    await waitFor(() => expect(requestsTo(fetchMock, 'POST', '/auth/activity')).toHaveLength(1))
    expect(headerOf(requestsTo(fetchMock, 'POST', '/auth/activity')[0], 'X-CSRF-Token')).toBe(
      'csrf-from-session',
    )
    now += 10 * ACTIVITY_INTERVAL_MS
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(requestsTo(fetchMock, 'POST', '/auth/activity')).toHaveLength(1)
  })
})

describe('sign-in failures', () => {
  it('explains a failed sign-in on the login page without echoing the code', async () => {
    mockFetch({ 'GET /auth/session': { body: SIGNED_OUT } })
    const { router } = renderRoute('/organizations?auth_error=%3Cscript%3E')
    expect(await screen.findByText('Sign-in failed')).toBeInTheDocument()
    expect(screen.getByText(/could not be verified/)).toBeInTheDocument()
    expect(screen.queryByText(/script/)).not.toBeInTheDocument()
    // The error code is not carried back to the page after signing in.
    expect(router.state.location.search).toBe('?auth_error=%3Cscript%3E&return_to=%2Forganizations')
  })

  it('tells an authenticated but unapproved visitor what to do, and nothing else', async () => {
    mockFetch({ 'GET /auth/session': { body: SIGNED_OUT } })
    renderRoute('/?auth_error=not_admitted')
    expect(await screen.findByText(/not approved to use Inquest/)).toBeInTheDocument()
    expect(screen.getByText(/open the invitation link again/)).toBeInTheDocument()
  })
})

describe('organizations', () => {
  it('lists memberships and switches organization from the header', async () => {
    mockFetch({
      'GET /auth/session': { body: SIGNED_IN },
      'GET /me': {
        body: {
          user: USER,
          memberships: [membership(ORG, 'Platform'), membership(ORG_2, 'Data Team', 'member')],
        },
      },
      [`GET /organizations/${ORG_2}`]: {
        body: { ...organization('member'), id: ORG_2, name: 'Data Team' },
      },
      [`GET /organizations/${ORG_2}/members`]: { body: members },
    })
    const { router } = renderRoute('/organizations')
    const list = await screen.findByRole('list', { name: 'Your organizations' })
    expect(within(list).getByText('Data Team')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /switch organization/i }))
    await userEvent.click(await screen.findByRole('menuitem', { name: /Data Team/ }))
    await waitFor(() => expect(router.state.location.pathname).toBe(`/organizations/${ORG_2}`))
    expect(screen.getByRole('button', { name: /switch organization/i })).toHaveTextContent(
      'Data Team',
    )
    expect(window.localStorage.getItem('inquest.organization')).toContain(ORG_2)
    // A member sees no invitation controls.
    expect(await screen.findByRole('list', { name: 'Members' })).toBeInTheDocument()
    expect(screen.queryByText('Invitations')).not.toBeInTheDocument()
  })

  it('creates an organization and opens it', async () => {
    const fetchMock = mockFetch({
      'GET /auth/session': { body: SIGNED_IN },
      'GET /me': { body: { user: USER, memberships: [] } },
      'POST /organizations': { status: 201, body: organization() },
      [`GET /organizations/${ORG}`]: { body: organization() },
      [`GET /organizations/${ORG}/members`]: { body: members },
      [`GET /organizations/${ORG}/invitations`]: { body: [] },
    })
    const { router } = renderRoute('/organizations')
    await userEvent.type(await screen.findByLabelText('New organization'), 'Platform')
    await userEvent.click(screen.getByRole('button', { name: /create/i }))
    await waitFor(() => expect(router.state.location.pathname).toBe(`/organizations/${ORG}`))
    const [call] = requestsTo(fetchMock, 'POST', '/organizations')
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({ name: 'Platform' })
    expect(headerOf(call, 'X-CSRF-Token')).toBe('csrf-from-session')
  })

  it('shows not found for an organization the user does not belong to', async () => {
    mockFetch({
      'GET /auth/session': { body: SIGNED_IN },
      'GET /me': { body: { user: USER, memberships: [] } },
      [`GET /organizations/${ORG}`]: { status: 404, body: { detail: 'organization not found' } },
      [`GET /organizations/${ORG}/members`]: {
        status: 404,
        body: { detail: 'organization not found' },
      },
    })
    renderRoute(`/organizations/${ORG}`)
    expect(await screen.findByText('Organization not found')).toBeInTheDocument()
  })

  it('invites a teammate, shows the link once, and revokes a pending invitation', async () => {
    const fetchMock = mockFetch({
      'GET /auth/session': { body: SIGNED_IN },
      'GET /me': { body: { user: USER, memberships: [membership(ORG, 'Platform')] } },
      [`GET /organizations/${ORG}`]: { body: organization() },
      [`GET /organizations/${ORG}/members`]: { body: members },
      [`GET /organizations/${ORG}/invitations`]: { body: [pending] },
      [`POST /organizations/${ORG}/invitations`]: {
        status: 201,
        body: {
          ...pending,
          email: 'dee@example.com',
          accept_url: 'https://app.test/invitations/accept#token=secret',
        },
      },
      [`DELETE /organizations/${ORG}/invitations/${INVITE}`]: { status: 204 },
    })
    renderRoute(`/organizations/${ORG}`)
    await userEvent.type(await screen.findByLabelText('Email'), 'dee@example.com')
    await userEvent.selectOptions(screen.getByLabelText('Role'), 'admin')
    await userEvent.click(screen.getByRole('button', { name: /^invite$/i }))
    expect(await screen.findByLabelText('Invitation link')).toHaveValue(
      'https://app.test/invitations/accept#token=secret',
    )
    expect(screen.getByText(/shown only now/)).toBeInTheDocument()
    const [call] = requestsTo(fetchMock, 'POST', `/organizations/${ORG}/invitations`)
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({
      email: 'dee@example.com',
      role: 'admin',
    })
    await userEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(screen.queryByLabelText('Invitation link')).not.toBeInTheDocument()

    await userEvent.click(
      screen.getByRole('button', { name: 'Revoke invitation for cy@example.com' }),
    )
    await waitFor(() =>
      expect(
        requestsTo(fetchMock, 'DELETE', `/organizations/${ORG}/invitations/${INVITE}`),
      ).toHaveLength(1),
    )
  })

  it('does not offer admin invitations or role changes to an admin', async () => {
    mockFetch({
      'GET /auth/session': { body: SIGNED_IN },
      'GET /me': { body: { user: USER, memberships: [membership(ORG, 'Platform', 'admin')] } },
      [`GET /organizations/${ORG}`]: { body: organization('admin') },
      [`GET /organizations/${ORG}/members`]: {
        body: [
          { ...members[0], role: 'admin' },
          members[1],
          { ...members[1], user_id: ORG_2, display_name: 'Eve', role: 'owner' },
        ],
      },
      [`GET /organizations/${ORG}/invitations`]: { body: [] },
    })
    renderRoute(`/organizations/${ORG}`)
    const role = await screen.findByLabelText('Role')
    expect(within(role).queryByRole('option', { name: 'Admin' })).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: /Role of/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove Bob' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove Eve' })).not.toBeInTheDocument()
  })
})

describe('accepting invitations', () => {
  it('reads the token from the fragment, removes it from the address bar and accepts', async () => {
    window.history.replaceState(null, '', '/invitations/accept#token=one-time-token')
    const fetchMock = mockFetch({
      'GET /auth/session': { body: SIGNED_IN },
      'GET /me': { body: { user: USER, memberships: [] } },
      'POST /invitations/accept': { body: membership(ORG, 'Platform', 'member') },
      [`GET /organizations/${ORG}`]: { body: organization('member') },
      [`GET /organizations/${ORG}/members`]: { body: members },
    })
    const { router } = renderRoute('/invitations/accept')
    await userEvent.click(await screen.findByRole('button', { name: 'Accept invitation' }))
    await waitFor(() => expect(router.state.location.pathname).toBe(`/organizations/${ORG}`))
    expect(window.location.hash).toBe('')
    const [call] = requestsTo(fetchMock, 'POST', '/invitations/accept')
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({ token: 'one-time-token' })
    expect(headerOf(call, 'X-CSRF-Token')).toBe('csrf-from-session')
  })

  it('signs in with the token in the request body, never a URL, and keeps it for this tab', async () => {
    window.history.replaceState(null, '', '/invitations/accept#token=one-time-token')
    const fetchMock = mockFetch({
      'GET /auth/session': { body: SIGNED_OUT },
      'POST /auth/login': { body: { authorization_url: 'https://idp.test/authorize?state=s' } },
    })
    const assign = vi.spyOn(pageNavigation, 'assign').mockImplementation(() => {})
    renderRoute('/invitations/accept')
    await userEvent.click(await screen.findByRole('button', { name: 'Sign in to continue' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://idp.test/authorize?state=s'))
    const [call] = requestsTo(fetchMock, 'POST', '/auth/login')
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({
      invitation_token: 'one-time-token',
      return_to: '/invitations/accept',
    })
    expect(String(call?.[0])).not.toContain('one-time-token')
    expect(window.sessionStorage.getItem('inquest.pendingInvitation')).toBe('one-time-token')
    expect(window.localStorage.length).toBe(0)
  })

  it('explains when the sign-in cannot start and lets the visitor retry', async () => {
    window.history.replaceState(null, '', '/invitations/accept#token=one-time-token')
    mockFetch({
      'GET /auth/session': { body: SIGNED_OUT },
      'POST /auth/login': {
        status: 429,
        body: { detail: { code: 'rate_limited', message: 'too many requests; try again later' } },
      },
    })
    const assign = vi.spyOn(pageNavigation, 'assign').mockImplementation(() => {})
    renderRoute('/invitations/accept')
    await userEvent.click(await screen.findByRole('button', { name: 'Sign in to continue' }))
    expect(await screen.findByRole('button', { name: 'Sign in to continue' })).toBeEnabled()
    expect(assign).not.toHaveBeenCalled()
  })

  it('opens the organization a new user joined by signing in from the invitation', async () => {
    window.sessionStorage.setItem('inquest.pendingInvitation', 'one-time-token')
    window.history.replaceState(null, '', `/invitations/accept?joined=${ORG}`)
    const fetchMock = mockFetch({
      'GET /auth/session': { body: SIGNED_IN },
      'GET /me': { body: { user: USER, memberships: [membership(ORG, 'Platform', 'member')] } },
      [`GET /organizations/${ORG}`]: { body: organization('member') },
      [`GET /organizations/${ORG}/members`]: { body: members },
    })
    const { router } = renderRoute(`/invitations/accept?joined=${ORG}`)
    await waitFor(() => expect(router.state.location.pathname).toBe(`/organizations/${ORG}`))
    expect(requestsTo(fetchMock, 'POST', '/invitations/accept')).toHaveLength(0)
    expect(window.sessionStorage.getItem('inquest.pendingInvitation')).toBeNull()
  })

  it('explains a used, expired or revoked invitation and offers no retry', async () => {
    window.history.replaceState(null, '', '/invitations/accept#token=used')
    mockFetch({
      'GET /auth/session': { body: SIGNED_IN },
      'GET /me': { body: { user: USER, memberships: [] } },
      'POST /invitations/accept': {
        status: 410,
        body: { detail: 'invitation is no longer valid' },
      },
    })
    renderRoute('/invitations/accept')
    await userEvent.click(await screen.findByRole('button', { name: 'Accept invitation' }))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Accept invitation' })).not.toBeInTheDocument(),
    )
  })

  it('shows a missing-token message without calling the API', async () => {
    window.history.replaceState(null, '', '/invitations/accept')
    const fetchMock = mockFetch({
      'GET /auth/session': { body: SIGNED_IN },
      'GET /me': { body: { user: USER, memberships: [] } },
    })
    renderRoute('/invitations/accept')
    expect(await screen.findByText('No invitation in this link')).toBeInTheDocument()
    expect(requestsTo(fetchMock, 'POST', '/invitations/accept')).toHaveLength(0)
  })
})

describe('permission model', () => {
  it('keeps credentials with environment owners, not organization administrators', async () => {
    mockFetch({})
    renderRoute('/organizations')
    const table = await screen.findByRole('table', { name: /permissions by role/i })
    const row = within(table).getByRole('row', { name: /Nebius and telemetry credentials/ })
    expect(
      within(row)
        .getAllByLabelText(/allowed/i)
        .map((c) => c.getAttribute('aria-label')),
    ).toEqual(['Not allowed', 'Not allowed', 'Not allowed', 'Allowed'])
    expect(within(row).getByText('Enforced')).toBeInTheDocument()
    const credentials = PERMISSIONS.find((p) => p.action.includes('credentials'))!
    expect([credentials.owner, credentials.admin, credentials.enforced]).toEqual([
      false,
      false,
      true,
    ])
  })

  it('never lets an invitation grant ownership', () => {
    expect(inviteInput.safeParse({ email: 'a@example.com', role: 'owner' }).success).toBe(false)
    expect(inviteInput.safeParse({ email: 'not-an-email', role: 'member' }).success).toBe(false)
    expect(inviteInput.safeParse({ email: 'a@example.com', role: 'admin' }).success).toBe(true)
  })
})
