import { createContext, useContext, type ReactNode } from 'react'

// Placeholder for organization selection (D025). The backend has no organizations,
// memberships or authentication yet, so there is nothing to select and nothing is
// invented here. When it exists, the selection is a UI preference only: the server
// decides what a user may see (docs/multi-tenancy.md).

export type Organization = { id: string; name: string }

type OrganizationState = {
  available: false
  organizations: readonly Organization[]
  selectedOrganization: Organization | null
}

const UNAVAILABLE: OrganizationState = {
  available: false,
  organizations: [],
  selectedOrganization: null,
}

const OrganizationContext = createContext<OrganizationState | null>(null)

export function OrganizationProvider({ children }: { children: ReactNode }) {
  return <OrganizationContext value={UNAVAILABLE}>{children}</OrganizationContext>
}

export function useOrganization() {
  const context = useContext(OrganizationContext)
  if (!context) throw new Error('useOrganization must be used inside OrganizationProvider')
  return context
}
