import { FileText } from 'lucide-react'
import { Link } from 'react-router'

import { StatusBadge } from '@/components/status-badge'
import type { InvestigationRead } from '@/lib/api'
import { formatDateTime, humanize } from '@/lib/format'
import type { StreamStatus } from '@/lib/sse/use-event-stream'

const STREAM_LABEL: Record<StreamStatus, string> = {
  connecting: 'Connecting to live activity…',
  open: 'Live activity connected',
  reconnecting: 'Reconnecting to live activity…',
  closed: 'Live activity disconnected',
  revoked: 'Live activity ended: access was removed',
}

export function InvestigationDetails({
  investigation,
  environments,
  stream,
}: {
  investigation: InvestigationRead
  environments: string[]
  stream: StreamStatus
}) {
  const reports = [...investigation.reports].sort((a, b) => b.version - a.version)
  return (
    <div className="space-y-5 text-sm">
      <dl className="grid grid-cols-2 gap-3 xl:grid-cols-1">
        <div>
          <dt className="text-xs text-muted-foreground">Status</dt>
          <dd className="mt-0.5">
            <StatusBadge status={investigation.status} />
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Environment</dt>
          <dd className="mt-0.5">{environments.join(', ') || '—'}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Severity</dt>
          <dd className="mt-0.5">
            {investigation.severity ? humanize(investigation.severity) : '—'}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Created</dt>
          <dd className="mt-0.5">{formatDateTime(investigation.created_at)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Rounds</dt>
          <dd className="mt-0.5">{investigation.current_round}</dd>
        </div>
      </dl>
      <div>
        <h2 className="mb-2 text-xs font-medium text-muted-foreground">Report versions</h2>
        {reports.length === 0 ? (
          <p className="text-xs text-muted-foreground">No report yet.</p>
        ) : (
          <ul className="space-y-1">
            {reports.map((r) => (
              <li key={r.id}>
                <Link
                  to={`/investigations/${investigation.id}/reports/${r.version}`}
                  className="flex items-center gap-2 rounded px-1.5 py-1 hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                >
                  <FileText aria-hidden className="size-3.5 text-muted-foreground" />
                  <span className="font-medium">v{r.version}</span>
                  <span className="text-xs text-muted-foreground">
                    {formatDateTime(r.created_at)}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
      <p className="text-xs text-muted-foreground" aria-live="polite">
        {STREAM_LABEL[stream]}
      </p>
    </div>
  )
}
