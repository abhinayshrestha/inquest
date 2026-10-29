import type { Location } from 'react-router'

import type { SignOutKind } from '@/app/contexts/auth'

export const LOGIN_PATH = '/login'

// Only same-site paths: anything else (absolute, protocol-relative, the login page itself)
// would let a crafted link send someone elsewhere after signing in.
export function safeReturnTo(value: string | null) {
  if (!value || !value.startsWith('/') || value.startsWith('//') || value.startsWith('/\\'))
    return '/'
  return value.startsWith(LOGIN_PATH) ? '/' : value
}

// The backend's OIDC callback reports failures as ?auth_error= on the app's root; carry it
// to the login page instead of into return_to.
export function loginPath(location: Location, ended: SignOutKind) {
  const params = new URLSearchParams()
  if (ended) {
    params.set('signed_out', ended)
  } else {
    const current = new URLSearchParams(location.search)
    const error = current.get('auth_error')
    current.delete('auth_error')
    if (error) params.set('auth_error', error)
    const search = current.size ? `?${current}` : ''
    const returnTo = `${location.pathname}${search}`
    if (returnTo !== '/') params.set('return_to', returnTo)
  }
  return params.size ? `${LOGIN_PATH}?${params}` : LOGIN_PATH
}
