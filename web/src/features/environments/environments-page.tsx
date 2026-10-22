import { Server } from 'lucide-react'

import { EmptyState } from '@/components/empty-state'
import { PageHeader } from '@/components/page-header'

export default function EnvironmentsPage() {
  return (
    <>
      <PageHeader
        title="Environments"
        description="Infrastructure Inquest investigates, with its telemetry scope and configuration."
      />
      <EmptyState
        icon={Server}
        title="Environment list not available yet"
        description={
          <>
            The backend can create environments but cannot list them yet. Creating environments here
            waits for environment ownership: only an environment&apos;s owner will configure its
            Nebius and telemetry credentials.
          </>
        }
        requires={['GET /environments']}
      />
    </>
  )
}
