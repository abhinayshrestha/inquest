import { Building2 } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link } from 'react-router'

import { useAuth } from '@/app/contexts/auth'
import { useOrganization } from '@/app/contexts/organization'
import { EmptyState } from '@/components/empty-state'
import { SectionSkeleton } from '@/components/skeletons'
import { Button } from '@/components/ui/button'

import { SignInRequired } from './sign-in-required'

// Pages that show one organization's data render only for a signed-in member with an
// organization selected. The server enforces the same (D033); this only avoids requests
// that would fail and explains why there is nothing to show.
export function RequireOrganization({ children }: { children: ReactNode }) {
  const { status } = useAuth()
  const { loading, selectedOrganization } = useOrganization()
  if (status !== 'signed_in') {
    return <SignInRequired what="see your organization's investigations and environments" />
  }
  if (loading) return <SectionSkeleton label="Loading your organizations" />
  if (!selectedOrganization) {
    return (
      <EmptyState
        icon={Building2}
        title="No organization yet"
        description="Environments, investigations and reports belong to an organization. Create one, or accept an invitation from a teammate."
        action={
          <Button asChild>
            <Link to="/organizations">Go to organizations</Link>
          </Button>
        }
      />
    )
  }
  return children
}

// A record of another of the user's organizations is not shown under the selected one:
// switching is explicit, and everything of the previous organization is dropped then.
export function InOrganization({
  organizationId,
  children,
}: {
  organizationId: string | null
  children: ReactNode
}) {
  const { organizations, selectedOrganization, selectOrganization } = useOrganization()
  if (organizationId !== null && organizationId === selectedOrganization?.organization_id) {
    return children
  }
  const owner = organizations.find((o) => o.organization_id === organizationId)
  return (
    <EmptyState
      icon={Building2}
      title="In another organization"
      description={
        owner
          ? `This belongs to ${owner.name}, not the organization you are viewing.`
          : 'This belongs to an organization you are not viewing.'
      }
      action={
        owner && (
          <Button onClick={() => selectOrganization(owner.organization_id)}>
            Switch to {owner.name}
          </Button>
        )
      }
    />
  )
}
