import { ChevronRight, Server } from 'lucide-react'
import { Link } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { ListSkeleton } from '@/components/skeletons'
import { Badge } from '@/components/ui/badge'
import { formatDateTime } from '@/lib/format'
import { cn } from 'cn'

import { useEnvironments } from './use-environments'

// Names, types, statuses and activity only: the list endpoint carries no configuration.
export function EnvironmentList({ compact = false }: { compact?: boolean }) {
  const environments = useEnvironments()
  if (environments.isPending) return <ListSkeleton rows={compact ? 2 : 4} />
  if (environments.error)
    return <ErrorState error={environments.error} onRetry={() => environments.refetch()} />
  if (environments.data.length === 0)
    return (
      <EmptyState
        icon={Server}
        title="No environments"
        description="This organization has no environments yet. Create one below; you become its owner."
      />
    )
  return (
    <ul className={cn('divide-y', !compact && 'rounded-lg border')} aria-label="Environments">
      {environments.data.map((e) => (
        <li key={e.id}>
          <Link
            to={`/environments/${e.id}`}
            className={cn(
              'flex items-center gap-3 hover:bg-accent/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset',
              compact ? '-mx-2 rounded-md px-2 py-2' : 'p-4',
            )}
          >
            <Server aria-hidden className="size-4 shrink-0 text-muted-foreground" />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">{e.name}</p>
              <p className="truncate text-xs text-muted-foreground">
                {e.type === 'aws' ? 'AWS' : 'Local'} · {e.investigation_count} investigation
                {e.investigation_count === 1 ? '' : 's'}
                {!compact &&
                  e.last_investigation_at &&
                  ` · last ${formatDateTime(e.last_investigation_at)}`}
              </p>
            </div>
            <Badge variant={e.status === 'active' ? 'secondary' : 'outline'}>{e.status}</Badge>
            <ChevronRight aria-hidden className="size-4 shrink-0 text-muted-foreground" />
          </Link>
        </li>
      ))}
    </ul>
  )
}
