import { Building2, Users } from 'lucide-react'

import { EmptyState } from '@/components/empty-state'
import { PageHeader } from '@/components/page-header'

export default function OrganizationsPage() {
  return (
    <>
      <PageHeader
        title="Organizations"
        description="Organizations, members and teammate invitations."
      />
      <div className="grid gap-4 lg:grid-cols-2">
        <EmptyState
          icon={Building2}
          badge="Planned"
          title="Organizations are not implemented"
          description="The backend has no users, organizations or authentication yet. Everything in this local setup is visible to anyone who can reach it, so run it locally only."
        />
        <EmptyState
          icon={Users}
          badge="Planned"
          title="Members and invitations"
          description="Organization owners will invite teammates by email. Invitations will be pending until accepted, revocable and expiring."
        />
      </div>
    </>
  )
}
