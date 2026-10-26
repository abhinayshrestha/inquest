import { Server } from 'lucide-react'

import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { ListSkeleton } from '@/components/skeletons'
import { cn } from 'cn'

import { useEnvironments } from './use-environments'

// Names, types and statuses only: the list endpoint deliberately carries no configuration.
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
        description="Create an environment through the backend API (POST /environments)."
      />
    )
  return (
    <ul className={cn('divide-y', !compact && 'rounded-lg border')} aria-label="Environments">
      {environments.data.map((e) => (
        <li key={e.id} className={cn('flex items-center gap-3', compact ? 'py-2' : 'p-4')}>
          <Server aria-hidden className="size-4 shrink-0 text-muted-foreground" />
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-medium">{e.name}</p>
            <p className="text-xs text-muted-foreground">{e.type === 'aws' ? 'AWS' : 'Local'}</p>
          </div>
          <span className="text-xs text-muted-foreground">{e.status}</span>
        </li>
      ))}
    </ul>
  )
}
