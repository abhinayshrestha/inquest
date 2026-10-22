import { ArrowLeft, MessagesSquare, SearchX } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link, useParams } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { PageHeader } from '@/components/page-header'
import { PageSkeleton } from '@/components/skeletons'
import { StatusBadge } from '@/components/status-badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { ApiError, type InvestigationRead } from '@/lib/api'
import { formatDateTime, humanize } from '@/lib/format'
import { isUuid } from '@/lib/ids'

import { useInvestigation } from './use-investigation'

function BackLink() {
  return (
    <Button asChild variant="ghost" size="sm" className="mb-2 -ml-2">
      <Link to="/investigations">
        <ArrowLeft aria-hidden />
        Investigations
      </Link>
    </Button>
  )
}

function NotFound() {
  return (
    <>
      <BackLink />
      <EmptyState
        icon={SearchX}
        title="Investigation not found"
        description="No investigation exists with this ID."
      />
    </>
  )
}

function Stat({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="rounded-lg border p-4">
      <dt className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{label}</dt>
      <dd className="mt-1 text-sm font-medium">{children}</dd>
    </div>
  )
}

function Summary({ investigation }: { investigation: InvestigationRead }) {
  const environments = [
    ...new Map(investigation.assignments.map((a) => [a.environment.id, a.environment])).values(),
  ]
  const latestReport = investigation.reports.at(-1)
  return (
    <div className="space-y-6">
      <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Status">
          <StatusBadge status={investigation.status} />
        </Stat>
        <Stat label="Severity">
          {investigation.severity ? humanize(investigation.severity) : '—'}
        </Stat>
        <Stat label="Created">{formatDateTime(investigation.created_at)}</Stat>
        <Stat label="Report">{latestReport ? `Version ${latestReport.version}` : 'None yet'}</Stat>
      </dl>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Initial question</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <p className="whitespace-pre-wrap">{investigation.question}</p>
          <p className="text-muted-foreground">
            {environments.length > 0
              ? `Environment: ${environments.map((e) => e.name).join(', ')}`
              : 'No environment assigned yet.'}
            {' · '}
            {investigation.turns.length} turn{investigation.turns.length === 1 ? '' : 's'} ·{' '}
            {investigation.current_round} round{investigation.current_round === 1 ? '' : 's'}
          </p>
        </CardContent>
      </Card>

      <EmptyState
        icon={MessagesSquare}
        badge="Next phase"
        title="Conversation view coming next"
        description="Turns, live investigation activity, evidence-cited answers and report versions will be shown here. The backend already provides them; the view is built in the next frontend phase."
      />
    </div>
  )
}

export default function InvestigationDetailPage() {
  const { id } = useParams()
  const valid = isUuid(id)
  const query = useInvestigation(id ?? '', { enabled: valid })

  if (!valid) return <NotFound />
  if (query.isPending) return <PageSkeleton variant="detail" />
  if (query.error) {
    if (query.error instanceof ApiError && query.error.kind === 'not_found') return <NotFound />
    return (
      <>
        <BackLink />
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
      </>
    )
  }

  const investigation = query.data
  return (
    <>
      <BackLink />
      <PageHeader title={investigation.title} description={`Investigation ${investigation.id}`} />
      <Summary investigation={investigation} />
    </>
  )
}
