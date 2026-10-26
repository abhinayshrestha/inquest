import { ArrowLeft, Server } from 'lucide-react'
import { Link, useParams } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { PageHeader } from '@/components/page-header'
import { Button } from '@/components/ui/button'

export default function EnvironmentDetailPage() {
  const { id } = useParams()
  return (
    <>
      <Button asChild variant="ghost" size="sm" className="mb-2 -ml-2">
        <Link to="/environments">
          <ArrowLeft aria-hidden />
          Environments
        </Link>
      </Button>
      <PageHeader title="Environment" description={id ? `Environment ${id}` : undefined} />
      <EmptyState
        icon={Server}
        title="Environment details not available yet"
        description="Telemetry scope, owner and configuration status will be shown here. Credentials are never displayed; owners will only be able to set or replace them."
        requires={['GET /environments/{id}']}
      />
    </>
  )
}
