import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createContext, useContext, useEffect, useMemo, type ReactNode } from 'react'

import { ApiError, setCsrfToken } from '@/lib/api/client'
import { getSession, logout, signInUrl, type User } from '@/lib/api/organizations'

// Who is signed in, from the backend session (an HttpOnly cookie the page cannot read).
// The page never decides identity: it only shows what GET /auth/session reports, and the
// server checks the session and role on every request.

export type AuthStatus = 'loading' | 'unavailable' | 'unconfigured' | 'signed_out' | 'signed_in'

type AuthState = {
  status: AuthStatus
  user: User | null
  signIn: (returnTo?: string) => void
  signOut: () => Promise<void>
  signingOut: boolean
}

const AuthContext = createContext<AuthState | null>(null)

export const SESSION_KEY = ['auth', 'session'] as const

// Full-page navigation to the backend login. An object so tests can replace it: jsdom's
// window.location cannot be stubbed.
export const pageNavigation = { assign: (url: string) => window.location.assign(url) }

export function AuthProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient()
  const session = useQuery({
    queryKey: SESSION_KEY,
    queryFn: async () => {
      const info = await getSession()
      setCsrfToken(info.csrf_token)
      return info
    },
    staleTime: 60_000,
    // Notices an expired or revoked session when the user comes back to the tab.
    refetchOnWindowFocus: true,
    retry: 1,
  })
  // Any 401 means the session ended (expired, revoked, signed out elsewhere): re-read it so
  // the UI shows signed-out states instead of a page of errors.
  useEffect(() => {
    const ended = (error: unknown) => error instanceof ApiError && error.kind === 'unauthorized'
    const refresh = () => void client.invalidateQueries({ queryKey: SESSION_KEY })
    const queries = client.getQueryCache().subscribe((event) => {
      if (event.type === 'updated' && event.action.type === 'error' && ended(event.action.error))
        refresh()
    })
    const mutations = client.getMutationCache().subscribe((event) => {
      if (event.type === 'updated' && event.action.type === 'error' && ended(event.action.error))
        refresh()
    })
    return () => {
      queries()
      mutations()
    }
  }, [client])

  const signOut = useMutation({
    mutationFn: logout,
    onSettled: async () => {
      setCsrfToken(null)
      // Everything cached belonged to the signed-in user.
      client.removeQueries({ predicate: (q) => q.queryKey[0] !== 'auth' })
      await client.invalidateQueries({ queryKey: SESSION_KEY })
    },
  })

  const value = useMemo<AuthState>(() => {
    const data = session.data
    const status: AuthStatus = session.isPending
      ? 'loading'
      : !data
        ? 'unavailable'
        : !data.configured
          ? 'unconfigured'
          : data.authenticated
            ? 'signed_in'
            : 'signed_out'
    return {
      status,
      user: data?.authenticated ? data.user : null,
      signIn: (returnTo = window.location.pathname) => {
        pageNavigation.assign(signInUrl(returnTo))
      },
      signOut: async () => {
        await signOut.mutateAsync().catch(() => undefined)
      },
      signingOut: signOut.isPending,
    }
  }, [session.isPending, session.data, signOut])

  return <AuthContext value={value}>{children}</AuthContext>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}
