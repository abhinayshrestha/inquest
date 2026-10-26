import { FileText } from 'lucide-react'
import { Link } from 'react-router'

import { StatusBadge } from '@/components/status-badge'
import type { InvestigationListItem } from '@/lib/api'
import { formatDateTime, humanize } from '@/lib/format'

export function InvestigationRow({ item }: { item: InvestigationListItem }) {
  return (
    <li>
      <Link
        to={`/investigations/${item.id}`}
        className="flex flex-col gap-2 p-4 hover:bg-accent/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset sm:flex-row sm:items-center sm:gap-4"
      >
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium">{item.title}</p>
          <p className="truncate text-xs text-muted-foreground">
            {item.environments.map((e) => e.name).join(', ') || 'No environment'} ·{' '}
            {formatDateTime(item.created_at)} · {item.turn_count} turn
            {item.turn_count === 1 ? '' : 's'}
            {item.severity && ` · ${humanize(item.severity)}`}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-3">
          {item.latest_report_version !== null && (
            <span className="flex items-center gap-1 text-xs text-muted-foreground">
              <FileText aria-hidden className="size-3.5" />v{item.latest_report_version}
            </span>
          )}
          <StatusBadge status={item.status} />
        </div>
      </Link>
    </li>
  )
}
