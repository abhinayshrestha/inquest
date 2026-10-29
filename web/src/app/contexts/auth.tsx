import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

import { ApiError, setCsrfToken } from '@/lib/api/client'
import {
  getSession,
  logout,
  logoutEverywhere,
  recordActivity,
  signInUrl,
  startInvitationSignIn,
  type User,
} from '@/lib/api/organizations'

// Who is signed in, from the backend session (an HttpOnly cookie the page cannot read).
// The page never decides identity: it only shows what GET /auth/session reports, and the
// server checks the session and role on every request.

export type AuthStatus = 'loading' | 'unavailable' | 'unconfigured' | 'signed_out' | 'signed_in'
// How this page's session ended when the person ended it here; null for anything else
// (expired, revoked, signed out in another tab), which returns them to the page they were on.
export type SignOutKind = 'signed_out' | 'everywhere' | null

type AuthState = {
  status: AuthStatus
  user: User | null
  ended: SignOutKind
  signIn: (returnTo?: string) => void
  // Rejects when the sign-in could not be started (rate limit, sign-in unavailable).
  signInWithInvitation: (token: string, returnTo: string) => Promise<void>
  signOut: () => Promise<void>
  // Ends the sessions in every browser (D043); rejects when it failed, sessions unchanged.
  signOutEverywhere: () => Promise<void>
  signingOut: boolean
}

const AuthContext = createContext<AuthState | null>(null)

export const SESSION_KEY = ['auth', 'session'] as const

// Full-page navigation to the backend login. An object so tests can replace it: jsdom's
// window.location cannot be stubbed.
export const pageNavigation = { assign: (url: string) => window.location.assign(url) }

// Input that shows a person is at the page. Polling, refetches and the event stream never
// move the session's idle deadline (D043); this does, at most once a minute.
const ACTIVITY_EVENTS = ['pointerdown', 'keydown', 'wheel', 'touchstart'] as const
export const ACTIVITY_INTERVAL_MS = 60_000

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
    const unauthorized = (error: unknown) =>
      error instanceof ApiError && error.kind === 'unauthorized'
    const refresh = () => void client.invalidateQueries({ queryKey: SESSION_KEY })
    const queries = client.getQueryCache().subscribe((event) => {
      if (
        event.type === 'updated' &&
        event.action.type === 'error' &&
        unauthorized(event.action.error)
      )
        refresh()
    })
    const mutations = client.getMutationCache().subscribe((event) => {
      if (
        event.type === 'updated' &&
        event.action.type === 'error' &&
        unauthorized(event.action.error)
      )
        refresh()
    })
    return () => {
      queries()
      mutations()
    }
  }, [client])

  const forget = async () => {
    setCsrfToken(null)
    // Everything cached belonged to the signed-in user.
    client.removeQueries({ predicate: (q) => q.queryKey[0] !== 'auth' })
    await client.invalidateQueries({ queryKey: SESSION_KEY })
  }
  const [ended, setEnded] = useState<SignOutKind>(null)
  const signOut = useMutation({
    mutationFn: logout,
    onSuccess: () => setEnded('signed_out'),
    onSettled: forget,
  })
  // Only on success: after a failure the sessions, this one included, are unchanged.
  const signOutEverywhere = useMutation({
    mutationFn: logoutEverywhere,
    onSuccess: () => {
      setEnded('everywhere')
      return forget()
    },
  })

  const signedIn = session.data?.authenticated === true
  useEffect(() => {
    if (!signedIn) return
    let last = Date.now()
    const active = () => {
      if (document.visibilityState !== 'visible' || Date.now() - last < ACTIVITY_INTERVAL_MS) return
      last = Date.now()
      recordActivity().catch((error: unknown) => {
        if (error instanceof ApiError && error.kind === 'unauthorized')
          void client.invalidateQueries({ queryKey: SESSION_KEY })
      })
    }
    for (const name of ACTIVITY_EVENTS) window.addEventListener(name, active, { passive: true })
    return () => {
      for (const name of ACTIVITY_EVENTS) window.removeEventListener(name, active)
    }
  }, [client, signedIn])

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
      ended,
      signIn: (returnTo = window.location.pathname) => {
        pageNavigation.assign(signInUrl(returnTo))
      },
      signInWithInvitation: async (token, returnTo) => {
        const { authorization_url } = await startInvitationSignIn(token, returnTo)
        pageNavigation.assign(authorization_url)
      },
      signOut: async () => {
        await signOut.mutateAsync().catch(() => undefined)
      },
      signOutEverywhere: async () => {
        await signOutEverywhere.mutateAsync()
      },
      signingOut: signOut.isPending || signOutEverywhere.isPending,
    }
  }, [session.isPending, session.data, ended, signOut, signOutEverywhere])

  return <AuthContext value={value}>{children}</AuthContext>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}
