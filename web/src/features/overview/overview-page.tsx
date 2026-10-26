import { FileText, Plus, Search } from 'lucide-react'
import { Link } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { ErrorBoundary } from '@/components/error-boundary'
import { ErrorState } from '@/components/error-state'
import { PageHeader } from '@/components/page-header'
import { ListSkeleton } from '@/components/skeletons'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { EnvironmentList } from '@/features/environments/environment-list'
import { InvestigationRow } from '@/features/investigations/investigation-row'
import { useInvestigationList } from '@/features/investigations/queries'

import { BackendStatusCard } from './backend-status-card'

const RECENT = 5

function RecentInvestigations() {
  const list = useInvestigationList(RECENT)
  const items = list.data?.pages[0]?.items ?? []
  return (
    <Card className="lg:col-span-2">
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle className="text-base">Recent investigations</CardTitle>
        <Button asChild variant="ghost" size="sm">
          <Link to="/investigations">View all</Link>
        </Button>
      </CardHeader>
      <CardContent>
        {list.isPending ? (
          <ListSkeleton rows={3} />
        ) : list.error ? (
          <ErrorState error={list.error} onRetry={() => list.refetch()} />
        ) : items.length === 0 ? (
          <EmptyState
            icon={Search}
            title="No investigations yet"
            description="Start one by asking a question about an environment."
            action={
              <Button asChild size="sm">
                <Link to="/investigations/new">
                  <Plus aria-hidden />
                  New investigation
                </Link>
              </Button>
            }
          />
        ) : (
          <ul className="-mx-4 divide-y" aria-label="Recent investigations">
            {items.map((item) => (
              <InvestigationRow key={item.id} item={item} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

export default function OverviewPage() {
  return (
    <>
      <PageHeader
        title="Overview"
        description="Running and recent investigations, environments and reports."
        actions={
          <Button asChild>
            <Link to="/investigations/new">
              <Plus aria-hidden />
              New investigation
            </Link>
          </Button>
        }
      />
      <div className="grid gap-4 lg:grid-cols-3">
        <ErrorBoundary>
          <BackendStatusCard />
        </ErrorBoundary>
        <RecentInvestigations />
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Environments</CardTitle>
          </CardHeader>
          <CardContent>
            <EnvironmentList compact />
          </CardContent>
        </Card>
        <EmptyState
          className="lg:col-span-2"
          icon={FileText}
          title="Reports"
          description="Each investigation keeps versioned reports; open one from its investigation or the Reports page."
          action={
            <Button asChild variant="outline" size="sm">
              <Link to="/reports">Go to reports</Link>
            </Button>
          }
        />
      </div>
    </>
  )
}
