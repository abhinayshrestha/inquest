import { FileText, Search, Server } from 'lucide-react'

import { EmptyState } from '@/components/empty-state'
import { ErrorBoundary } from '@/components/error-boundary'
import { PageHeader } from '@/components/page-header'

import { BackendStatusCard } from './backend-status-card'

export default function OverviewPage() {
  return (
    <>
      <PageHeader
        title="Overview"
        description="Running and recent investigations, environments and reports."
      />
      <div className="grid gap-4 lg:grid-cols-3">
        <ErrorBoundary>
          <BackendStatusCard />
        </ErrorBoundary>
        <EmptyState
          className="lg:col-span-2"
          icon={Search}
          title="Recent investigations"
          description="Running and recent investigations will appear here once the backend can list them."
          requires={['GET /investigations']}
        />
        <EmptyState
          icon={Server}
          title="Environments"
          description="Environment configuration status will appear here."
          requires={['GET /environments']}
        />
        <EmptyState
          className="lg:col-span-2"
          icon={FileText}
          title="Latest reports"
          description="Latest report versions across investigations will appear here."
          requires={['GET /reports (or an equivalent list)']}
        />
      </div>
    </>
  )
}
