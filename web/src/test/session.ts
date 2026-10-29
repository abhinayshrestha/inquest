import { ORGANIZATION_ID } from '@/test/fixtures'

// Who tests are signed in as by default (test/render.tsx mockFetch).
export const SIGNED_OUT_UNCONFIGURED = {
  configured: false,
  authenticated: false,
  user: null,
  csrf_token: null,
  expires_at: null,
}
export const TEST_USER = {
  id: '00000000-0000-4000-8000-0000000000f1',
  email: 'tess@example.com',
  email_verified: true,
  display_name: 'Tess',
}
export const TEST_ORGANIZATION = ORGANIZATION_ID
export const SIGNED_IN = {
  configured: true,
  authenticated: true,
  user: TEST_USER,
  csrf_token: 'test-csrf-token',
  expires_at: '2099-01-01T00:00:00Z',
}
export const MEMBERSHIP = {
  organization_id: TEST_ORGANIZATION,
  name: 'Test Org',
  slug: 'test-org',
  role: 'owner',
}
