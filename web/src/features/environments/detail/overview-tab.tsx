import { Loader2, Plus, Search } from 'lucide-react'
import { Link } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { ListSkeleton } from '@/components/skeletons'
import { Button } from '@/components/ui/button'
import { InvestigationRow } from '@/features/investigations/investigation-row'
import { useInvestigationList } from '@/features/investigations/queries'
import type { EnvironmentDetail } from '@/lib/api'
import { formatDateTime } from '@/lib/format'

import { Section } from './sections'

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg border p-4">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-1 text-xl font-semibold tabular-nums">{value}</dd>
    </div>
  )
}

function EnvironmentInvestigations({ environment }: { environment: EnvironmentDetail }) {
  const list = useInvestigationList(10, environment.id)
  const items = list.data?.pages.flatMap((p) => p.items) ?? []
  const start =
    environment.status === 'active' ? (
      <Button asChild size="sm">
        <Link to={`/investigations/new?environment=${environment.id}`}>
          <Plus aria-hidden />
          New investigation
        </Link>
      </Button>
    ) : null
  return (
    <Section title="Investigations" badge={<span className="ml-auto">{start}</span>}>
      {list.isPending ? (
        <ListSkeleton rows={3} />
      ) : list.error ? (
        <ErrorState error={list.error} onRetry={() => list.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          icon={Search}
          title="No investigations yet"
          description="Investigations of this environment will be listed here."
        />
      ) : (
        <div className="space-y-3">
          <ul
            className="-mx-6 divide-y border-y"
            aria-label={`Investigations of ${environment.name}`}
          >
            {items.map((item) => (
              <InvestigationRow key={item.id} item={item} />
            ))}
          </ul>
          {list.hasNextPage && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => list.fetchNextPage()}
              disabled={list.isFetchingNextPage}
            >
              {list.isFetchingNextPage && <Loader2 aria-hidden className="animate-spin" />}
              Load more
            </Button>
          )}
        </div>
      )}
    </Section>
  )
}

export function OverviewTab({ environment }: { environment: EnvironmentDetail }) {
  const { stats } = environment
  return (
    <div className="space-y-4">
      <dl className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Investigations" value={stats.investigations} />
        <Stat label="Running now" value={stats.active_investigations} />
        <Stat label="Evidence recorded" value={stats.evidence} />
        <Stat label="Telemetry queries" value={stats.tool_calls} />
      </dl>
      <p className="text-xs text-muted-foreground">
        Last investigation:{' '}
        {stats.last_investigation_at ? formatDateTime(stats.last_investigation_at) : 'none yet'}
      </p>
      <EnvironmentInvestigations environment={environment} />
    </div>
  )
}
