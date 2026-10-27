import { ArrowRight, FileText, Plus, Search, Server } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { Link } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { ErrorBoundary } from '@/components/error-boundary'
import { ErrorState } from '@/components/error-state'
import { PageHeader } from '@/components/page-header'
import { CardGridSkeleton, ListSkeleton } from '@/components/skeletons'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { InvestigationRow } from '@/features/investigations/investigation-row'
import { useInvestigationList } from '@/features/investigations/queries'
import type { InvestigationFilters, OverviewCounts } from '@/lib/api'
import { cn } from 'cn'

import { BackendStatusCard } from './backend-status-card'
import { useOverview } from './use-overview'

// Everything here is a count or list of stored records. Inquest measures no health,
// uptime or alert figures, so none are shown.

const RECENT = 5

const DESTINATIONS: { to: string; label: string; detail: string; icon: LucideIcon }[] = [
  {
    to: '/investigations',
    label: 'Investigations',
    detail: 'History and conversations',
    icon: Search,
  },
  {
    to: '/environments',
    label: 'Environments',
    detail: 'Scope, telemetry and providers',
    icon: Server,
  },
  { to: '/reports', label: 'Reports', detail: 'Versions across investigations', icon: FileText },
]

const STATUS_PARTS: {
  key: keyof OverviewCounts['investigations_by_status']
  label: string
  className: string
}[] = [
  { key: 'investigating', label: 'Investigating', className: 'bg-info' },
  { key: 'correlating', label: 'Correlating', className: 'bg-info/60' },
  { key: 'pending', label: 'Not started', className: 'bg-muted-foreground/40' },
  { key: 'idle', label: 'Open (answered or ready)', className: 'bg-success' },
  { key: 'completed', label: 'Closed', className: 'bg-success/50' },
  { key: 'failed', label: 'Failed', className: 'bg-destructive' },
  { key: 'cancelled', label: 'Cancelled', className: 'bg-warning' },
]

const TURN_PARTS: {
  key: keyof OverviewCounts['latest_turn_by_status']
  label: string
  className: string
}[] = [
  { key: 'running', label: 'Running', className: 'bg-info' },
  { key: 'pending', label: 'Not started', className: 'bg-muted-foreground/40' },
  { key: 'completed', label: 'Answered', className: 'bg-success' },
  { key: 'failed', label: 'Failed', className: 'bg-destructive' },
  { key: 'cancelled', label: 'Cancelled', className: 'bg-warning' },
]

function Tile({
  icon: Icon,
  label,
  value,
  detail,
  to,
}: {
  icon: LucideIcon
  label: string
  value: number
  detail: string
  to: string
}) {
  return (
    <Link
      to={to}
      className="group rounded-lg border bg-card p-4 hover:bg-accent/40 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
    >
      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span className="flex items-center gap-2">
          <Icon aria-hidden className="size-4" />
          {label}
        </span>
        <ArrowRight
          aria-hidden
          className="size-4 opacity-0 transition-opacity group-hover:opacity-100"
        />
      </div>
      <p className="mt-2 text-2xl font-semibold tabular-nums">{value}</p>
      <p className="text-xs text-muted-foreground">{detail}</p>
    </Link>
  )
}

function Breakdown<K extends string>({
  title,
  description,
  total,
  counts,
  parts,
}: {
  title: string
  description: string
  total: number
  counts: Record<K, number>
  parts: { key: K; label: string; className: string }[]
}) {
  const present = parts.filter((p) => (counts[p.key] ?? 0) > 0)
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {total === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing recorded yet.</p>
        ) : (
          <div className="flex h-2.5 overflow-hidden rounded-full bg-muted" aria-hidden>
            {present.map((p) => (
              <div
                key={p.key}
                className={p.className}
                style={{ width: `${(100 * counts[p.key]) / total}%` }}
              />
            ))}
          </div>
        )}
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
          {parts.map((p) => (
            <div key={p.key} className="flex items-center justify-between gap-2">
              <dt className="flex items-center gap-2 text-muted-foreground">
                <span aria-hidden className={cn('size-2 rounded-full', p.className)} />
                {p.label}
              </dt>
              <dd className="font-medium tabular-nums">{counts[p.key] ?? 0}</dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  )
}

function RecentList({
  title,
  empty,
  filters,
}: {
  title: string
  empty: string
  filters: Pick<InvestigationFilters, 'statuses' | 'latestTurnStatuses'>
}) {
  const list = useInvestigationList(RECENT, undefined, filters)
  const items = list.data?.pages[0]?.items ?? []
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        {list.isPending ? (
          <ListSkeleton rows={2} />
        ) : list.error ? (
          <ErrorState error={list.error} onRetry={() => list.refetch()} />
        ) : items.length === 0 ? (
          <p className="text-sm text-muted-foreground">{empty}</p>
        ) : (
          <ul className="-mx-6 divide-y border-y" aria-label={title}>
            {items.map((item) => (
              <InvestigationRow key={item.id} item={item} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

function Counts() {
  const overview = useOverview()
  if (overview.isPending) return <CardGridSkeleton cards={3} />
  if (overview.error)
    return <ErrorState error={overview.error} onRetry={() => overview.refetch()} />
  const c = overview.data
  const running = c.investigations_by_status.investigating + c.investigations_by_status.correlating
  if (c.investigations_total === 0) {
    return (
      <EmptyState
        icon={Search}
        title="No investigations yet"
        description="Ask Inquest why something is failing or slow; it gathers evidence and answers with citations."
        action={
          <Button asChild size="sm">
            <Link to="/investigations/new">
              <Plus aria-hidden />
              New investigation
            </Link>
          </Button>
        }
      />
    )
  }
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <Tile
          icon={Search}
          label="Investigations"
          value={c.investigations_total}
          detail={`${running} running now · ${c.turns_total} questions asked`}
          to="/investigations"
        />
        <Tile
          icon={Server}
          label="Environments"
          value={c.environments_total}
          detail={`${c.environments_active} active`}
          to="/environments"
        />
        <Tile
          icon={FileText}
          label="Report versions"
          value={c.reports_total}
          detail={`across ${c.investigations_with_reports} investigations`}
          to="/reports"
        />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Breakdown
          title="Investigations by status"
          description="An investigation stays open for follow-ups after each answer, so most are open."
          total={c.investigations_total}
          counts={c.investigations_by_status}
          parts={STATUS_PARTS}
        />
        <Breakdown
          title="Latest question per investigation"
          description="How each investigation's most recent question ended. A failed question leaves the investigation open."
          total={c.investigations_total}
          counts={c.latest_turn_by_status}
          parts={TURN_PARTS}
        />
      </div>
    </div>
  )
}

export default function OverviewPage() {
  return (
    <>
      <PageHeader
        title="Overview"
        description="Stored investigations, questions, environments and reports. Counts only; nothing here is a live measurement."
        actions={
          <Button asChild>
            <Link to="/investigations/new">
              <Plus aria-hidden />
              New investigation
            </Link>
          </Button>
        }
      />
      <div className="space-y-4">
        <Counts />
        <div className="grid gap-4 lg:grid-cols-3">
          <RecentList
            title="Running"
            empty="Nothing is running."
            filters={{ statuses: ['investigating', 'correlating'] }}
          />
          <RecentList
            title="Recently answered"
            empty="No answered questions yet."
            filters={{ latestTurnStatuses: ['completed'] }}
          />
          <RecentList
            title="Last question failed or cancelled"
            empty="None."
            filters={{ latestTurnStatuses: ['failed', 'cancelled'] }}
          />
        </div>
        <div className="grid gap-4 lg:grid-cols-3">
          <ErrorBoundary>
            <BackendStatusCard />
          </ErrorBoundary>
          <Card className="lg:col-span-2">
            <CardHeader className="flex flex-row items-center justify-between">
              <CardTitle className="text-base">Go to</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-2 sm:grid-cols-3">
              {DESTINATIONS.map(({ to, label, detail, icon: Icon }) => (
                <Link
                  key={to}
                  to={to}
                  className="flex items-start gap-3 rounded-md border p-3 hover:bg-accent/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                >
                  <Icon aria-hidden className="mt-0.5 size-4 text-muted-foreground" />
                  <span>
                    <span className="block text-sm font-medium">{label}</span>
                    <span className="block text-xs text-muted-foreground">{detail}</span>
                  </span>
                </Link>
              ))}
            </CardContent>
          </Card>
        </div>
      </div>
    </>
  )
}
