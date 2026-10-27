import { useQuery, useQueryClient, type Query } from '@tanstack/react-query'
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import { ApiError, TENANT_ROOTS } from '@/lib/api'
import { getMe, type Membership } from '@/lib/api/organizations'
import { readPreference, writePreference } from '@/lib/storage'

import { useAuth } from './auth'

// The signed-in user's organizations and which one the UI is showing. The selection is a
// display preference only (stored as an id in this browser): the server never takes it
// as proof of anything and checks membership on every request (D033).

type OrganizationState = {
  available: boolean
  loading: boolean
  organizations: readonly Membership[]
  selectedOrganization: Membership | null
  selectOrganization: (organizationId: string) => void
}

const OrganizationContext = createContext<OrganizationState | null>(null)

export const ME_KEY = ['me'] as const

const LIST_ROOTS = new Set(['overview', 'reports', 'environments', 'investigations'])

export function OrganizationProvider({ children }: { children: ReactNode }) {
  const { status } = useAuth()
  const client = useQueryClient()
  const signedIn = status === 'signed_in'
  const me = useQuery({ queryKey: ME_KEY, queryFn: getMe, enabled: signedIn })
  const [preferred, setPreferred] = useState(() => readPreference('organization'))

  const organizations = useMemo(
    () => (signedIn ? (me.data?.memberships ?? []) : []),
    [signedIn, me.data],
  )
  const selected =
    organizations.find((o) => o.organization_id === preferred) ?? organizations[0] ?? null
  const selectedId = selected?.organization_id ?? null

  // Data of one organization never stays on screen for another: whenever the effective
  // organization changes (switch, removal, sign-in as someone else) tenant data is dropped
  // and refetched for the new one. Open event streams close with their pages.
  const shown = useRef(selectedId)
  useEffect(() => {
    if (shown.current === selectedId) return
    const first = shown.current === null
    shown.current = selectedId
    if (first) return // nothing was shown for an organization yet
    const predicate = (q: Query) => TENANT_ROOTS.has(q.queryKey[0])
    client.removeQueries({ predicate, type: 'inactive' })
    // Mounted pages keep their queries but lose the data at once, then refetch. Lists of
    // the new organization are new queries already.
    void client.resetQueries({
      predicate: (q) => predicate(q) && q.queryKey[1] !== selectedId,
      type: 'active',
    })
  }, [client, selectedId])

  // A collection answering 404 for the selected organization means the membership is
  // gone (removed by an admin while signed in): refresh memberships so the UI follows.
  useEffect(
    () =>
      client.getQueryCache().subscribe((event) => {
        const error = event.query.state.error
        if (
          event.type === 'updated' &&
          event.action.type === 'error' &&
          LIST_ROOTS.has(event.query.queryKey[0]) &&
          error instanceof ApiError &&
          error.kind === 'not_found'
        ) {
          void client.invalidateQueries({ queryKey: ME_KEY })
        }
      }),
    [client],
  )

  const selectOrganization = useCallback((organizationId: string) => {
    writePreference('organization', organizationId)
    setPreferred(organizationId)
  }, [])

  const value = useMemo<OrganizationState>(
    () => ({
      available: signedIn,
      loading: signedIn && me.isPending,
      organizations,
      selectedOrganization: selected,
      selectOrganization,
    }),
    [signedIn, me.isPending, organizations, selected, selectOrganization],
  )

  return <OrganizationContext value={value}>{children}</OrganizationContext>
}

export function useOrganization() {
  const context = useContext(OrganizationContext)
  if (!context) throw new Error('useOrganization must be used inside OrganizationProvider')
  return context
}

// The organization whose data pages show; null while signed out or without one.
export function useOrganizationId() {
  return useOrganization().selectedOrganization?.organization_id ?? null
}
