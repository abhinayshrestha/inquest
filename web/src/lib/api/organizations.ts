import { z } from '@/lib/zod'

import { request, API_BASE } from './client'

// Identity and organizations (Phase 7A): mirrors src/schemas/identity.py. The session is
// an HttpOnly cookie the page cannot read; what the page learns about the user comes
// from GET /auth/session and GET /me. Nothing here is trusted by the server: it checks
// membership and role on every request.

const id = z.uuid()
const timestamp = z.iso.datetime({ offset: true })

export const organizationRole = z.enum(['owner', 'admin', 'member'])
export const invitationStatus = z.enum(['pending', 'accepted', 'revoked', 'expired'])

export const user = z.object({
  id,
  email: z.string().nullable(),
  email_verified: z.boolean(),
  display_name: z.string().nullable(),
})

export const sessionInfo = z.object({
  configured: z.boolean(),
  authenticated: z.boolean(),
  user: user.nullable(),
  csrf_token: z.string().nullable(),
  expires_at: timestamp.nullable(),
})

export const membership = z.object({
  organization_id: id,
  name: z.string(),
  slug: z.string(),
  role: organizationRole,
})

export const me = z.object({ user, memberships: z.array(membership) })

export const organization = z.object({
  id,
  name: z.string(),
  slug: z.string(),
  created_at: timestamp,
  role: organizationRole,
  member_count: z.int(),
})

export const member = z.object({
  user_id: id,
  email: z.string().nullable(),
  display_name: z.string().nullable(),
  role: organizationRole,
  joined_at: timestamp,
})

export const invitation = z.object({
  id,
  email: z.string(),
  role: organizationRole,
  status: invitationStatus,
  invited_by: z.string().nullable(),
  created_at: timestamp,
  expires_at: timestamp,
})

export const invitationCreated = invitation.extend({ accept_url: z.string() })

export const inviteInput = z.strictObject({
  email: z.email('Enter a valid email address'),
  role: organizationRole.exclude(['owner']),
})

export type OrganizationRole = z.infer<typeof organizationRole>
export type User = z.infer<typeof user>
export type SessionInfo = z.infer<typeof sessionInfo>
export type Membership = z.infer<typeof membership>
export type Me = z.infer<typeof me>
export type Organization = z.infer<typeof organization>
export type Member = z.infer<typeof member>
export type Invitation = z.infer<typeof invitation>
export type InvitationCreated = z.infer<typeof invitationCreated>
export type InviteInput = z.infer<typeof inviteInput>

const enc = encodeURIComponent

export const getSession = () => request('/auth/session', sessionInfo)
export const logout = () => request('/auth/logout', z.null(), { method: 'POST' })
// Ends every session of the signed-in user, this one included (D043).
export const logoutEverywhere = () =>
  request('/auth/logout-everywhere', z.null(), { method: 'POST' })
// Real user input: the only reads-free way the idle deadline moves (D043).
export const recordActivity = () => request('/auth/activity', z.null(), { method: 'POST' })
export const getMe = () => request('/me', me)
export const createOrganization = (name: string) =>
  request('/organizations', organization, { method: 'POST', json: { name } })
export const getOrganization = (orgId: string) =>
  request(`/organizations/${enc(orgId)}`, organization)
export const listMembers = (orgId: string) =>
  request(`/organizations/${enc(orgId)}/members`, z.array(member))
export const changeRole = (orgId: string, userId: string, role: OrganizationRole) =>
  request(`/organizations/${enc(orgId)}/members/${enc(userId)}`, member, {
    method: 'PATCH',
    json: { role },
  })
export const removeMember = (orgId: string, userId: string) =>
  request(`/organizations/${enc(orgId)}/members/${enc(userId)}`, z.null(), { method: 'DELETE' })
export const listInvitations = (orgId: string) =>
  request(`/organizations/${enc(orgId)}/invitations`, z.array(invitation))
export const createInvitation = (orgId: string, input: InviteInput) =>
  request(`/organizations/${enc(orgId)}/invitations`, invitationCreated, {
    method: 'POST',
    json: input,
  })
export const revokeInvitation = (orgId: string, invitationId: string) =>
  request(`/organizations/${enc(orgId)}/invitations/${enc(invitationId)}`, z.null(), {
    method: 'DELETE',
  })
export const acceptInvitation = (token: string) =>
  request('/invitations/accept', membership, { method: 'POST', json: { token } })

const signInStart = z.object({ authorization_url: z.url({ protocol: /^https?$/ }) })

// Sign-in from an invitation link (D042): the token goes in the request body, never in a
// URL. If the provider-verified identity is not yet allowed to use Inquest, the backend
// admits it by accepting this invitation when the provider sends the browser back.
export const startInvitationSignIn = (invitationToken: string, returnTo: string) =>
  request('/auth/login', signInStart, {
    method: 'POST',
    json: { invitation_token: invitationToken, return_to: returnTo },
  })

// A full-page navigation: the backend redirects to the provider and back.
export function signInUrl(returnTo = '/') {
  return `${API_BASE}/auth/login?return_to=${enc(returnTo)}`
}

// Organization administration and environment ownership are separate: owners and admins
// manage people, while only an environment's owner sets its credentials (D029).
// `enforced` marks what the server checks today (all of it since Phase 7C-3).
export type Permission = {
  action: string
  owner: boolean
  admin: boolean
  member: boolean
  environmentOwner: boolean
  enforced: boolean
}

export const PERMISSIONS: readonly Permission[] = [
  { action: 'See the organization and its members', owner: true, admin: true, member: true, environmentOwner: true, enforced: true },
  { action: 'Invite members', owner: true, admin: true, member: false, environmentOwner: false, enforced: true },
  { action: 'Invite admins', owner: true, admin: false, member: false, environmentOwner: false, enforced: true },
  { action: 'Remove members', owner: true, admin: true, member: false, environmentOwner: false, enforced: true },
  { action: 'Change roles', owner: true, admin: false, member: false, environmentOwner: false, enforced: true },
  { action: 'Create environments (creator becomes owner)', owner: true, admin: true, member: true, environmentOwner: true, enforced: true },
  { action: "Set an environment's Nebius and telemetry credentials", owner: false, admin: false, member: false, environmentOwner: true, enforced: true },
  { action: 'Transfer environment ownership', owner: false, admin: false, member: false, environmentOwner: true, enforced: true },
] // prettier-ignore
