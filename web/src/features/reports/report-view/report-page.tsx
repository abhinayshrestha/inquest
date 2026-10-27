import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  FileText,
  FileX,
  Info,
  ListChecks,
} from 'lucide-react'
import type { ReactNode } from 'react'
import { Link, useParams } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { PageHeader } from '@/components/page-header'
import { PageSkeleton } from '@/components/skeletons'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { CitationSheet } from '@/features/investigations/conversation/citation-sheet'
import { useCitationParam } from '@/features/investigations/conversation/use-citation-param'
import { useInvestigation, usePresentation, useReport } from '@/features/investigations/queries'
import { InOrganization } from '@/features/organizations/organization-gate'
import { ApiError, reportContent, type InvestigationRead, type ReportContent } from '@/lib/api'
import { formatDateTime } from '@/lib/format'
import { ago, formatUtc, parseTime } from '@/lib/freshness'
import { isUuid } from '@/lib/ids'
import { ResponseBlocks } from '@/lib/render/blocks/response-blocks'
import { CitationGroup } from '@/lib/render/citation-group'
import { Markdown } from '@/lib/render/markdown'
import { cn } from 'cn'

import { VersionChanges } from './changes'
import { CurrentConclusions, HistoricalConclusions } from './conclusions'
import { OUTCOME_LABELS } from './vocabulary'

function Block({ title, children, id }: { title: string; children: ReactNode; id?: string }) {
  return (
    <Card id={id}>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

// The span of time the report's evidence covers, so its age is explicit up front.
function FreshnessNotice({ report, createdAt }: { report: ReportContent; createdAt: string }) {
  const times = report.conclusions
    .map((c) => parseTime(c.as_of))
    .filter((t): t is number => t !== null)
  const latest = times.length ? Math.max(...times) : null
  return (
    <p className="flex items-start gap-2 rounded-lg border border-info/30 bg-info/10 px-3 py-2 text-sm">
      <Info aria-hidden className="mt-0.5 size-4 shrink-0 text-info" />
      <span>
        A record, not live telemetry.{' '}
        {latest !== null
          ? `Its conclusions rest on evidence observed up to ${formatUtc(latest)} (${ago(latest)}).`
          : 'Its conclusions do not record when their evidence was observed.'}{' '}
        Written {formatDateTime(createdAt)}. Ask a follow-up in the investigation to check the
        current state.
      </span>
    </p>
  )
}

// A link when the version exists, otherwise a disabled button with the same look.
function StepLink({
  to,
  label,
  children,
}: {
  to: string | null
  label: string
  children: ReactNode
}) {
  if (!to) {
    return (
      <Button variant="outline" size="sm" disabled>
        {children}
      </Button>
    )
  }
  return (
    <Button asChild variant="outline" size="sm">
      <Link to={to} aria-label={label}>
        {children}
      </Link>
    </Button>
  )
}

function VersionNav({
  investigation,
  version,
}: {
  investigation: InvestigationRead
  version: number
}) {
  const versions = [...investigation.reports].sort((a, b) => b.version - a.version)
  const base = `/investigations/${investigation.id}/reports`
  const has = (v: number) => versions.some((r) => r.version === v)
  return (
    <nav aria-label="Report versions" className="space-y-2">
      <div className="flex items-center gap-2">
        <StepLink to={has(version - 1) ? `${base}/${version - 1}` : null} label="Previous version">
          <ChevronLeft aria-hidden />
          Previous
        </StepLink>
        <StepLink to={has(version + 1) ? `${base}/${version + 1}` : null} label="Next version">
          Next
          <ChevronRight aria-hidden />
        </StepLink>
      </div>
      <ul className="flex gap-1 overflow-x-auto xl:flex-col">
        {versions.map((r) => (
          <li key={r.id} className="shrink-0">
            <Link
              to={`${base}/${r.version}`}
              aria-current={r.version === version ? 'page' : undefined}
              className={cn(
                'flex items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
                r.version === version && 'bg-accent font-medium',
              )}
            >
              <FileText aria-hidden className="size-3.5 text-muted-foreground" />v{r.version}
              <span className="text-xs text-muted-foreground">{formatDateTime(r.created_at)}</span>
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  )
}

// Charts, metrics and tables from the answer that produced this version, reused from the
// stored response blocks (D028); text blocks stay in the conversation.
function EvidenceViews({
  investigationId,
  turnId,
  onCite,
}: {
  investigationId: string
  turnId: string
  onCite: (ref: string) => void
}) {
  const presentation = usePresentation(investigationId, turnId)
  if (!presentation.isSuccess) return null
  const data = presentation.data.blocks.filter(
    (b) =>
      !!b &&
      typeof b === 'object' &&
      'type' in b &&
      ['metric', 'chart', 'table'].includes(String(b.type)),
  )
  if (data.length === 0) return null
  return (
    <Block title="Evidence views from the answer that produced this version">
      <ResponseBlocks blocks={data} onCite={onCite} />
    </Block>
  )
}

function ReportBody({
  investigation,
  version,
  createdAt,
  turnId,
  report,
}: {
  investigation: InvestigationRead
  version: number
  createdAt: string
  turnId: string
  report: ReportContent
}) {
  const { open } = useCitationParam()
  const latest = Math.max(...investigation.reports.map((r) => r.version))
  const environments = [...new Set(investigation.assignments.map((a) => a.environment.name))]
  return (
    <div className="xl:grid xl:grid-cols-[minmax(0,1fr)_14rem] xl:gap-8">
      <div className="min-w-0 space-y-4">
        <PageHeader
          title={`Report v${version}`}
          description={
            <span className="inline-flex flex-wrap items-center gap-2">
              <Link
                to={`/investigations/${investigation.id}`}
                className="font-medium text-foreground hover:underline"
              >
                {investigation.title}
              </Link>
              {environments.length > 0 && <span>· {environments.join(', ')}</span>}
            </span>
          }
        />
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="secondary">{OUTCOME_LABELS[report.outcome] ?? report.outcome}</Badge>
          {version === latest ? (
            <Badge variant="outline">Latest version</Badge>
          ) : (
            <Badge variant="outline" className="text-warning">
              Superseded by v{latest}
            </Badge>
          )}
          <span className="text-xs text-muted-foreground">Versions are immutable.</span>
        </div>
        <div className="xl:hidden">
          <VersionNav investigation={investigation} version={version} />
        </div>
        <FreshnessNotice report={report} createdAt={createdAt} />

        <Block title="Summary">
          <Markdown onCite={open}>{report.summary}</Markdown>
        </Block>
        <Block title={version > 1 ? `Changes from v${version - 1}` : 'Changes'}>
          <VersionChanges report={report} version={version} onCite={open} />
        </Block>
        <Block title="Current conclusions">
          <CurrentConclusions conclusions={report.conclusions} onCite={open} />
        </Block>
        {report.history.length > 0 && (
          <Block title="Historical conclusions">
            <p className="mb-3 text-xs text-muted-foreground">
              Held by earlier versions and not restated here. Kept as observed at the time; they do
              not describe the present.
            </p>
            <HistoricalConclusions history={report.history} onCite={open} />
          </Block>
        )}
        {report.timeline.length > 0 && (
          <Block title="Timeline">
            <ol className="space-y-3 border-l pl-4">
              {report.timeline.map((t, i) => (
                <li key={i} className="space-y-1 text-sm">
                  <p className="font-mono text-xs text-muted-foreground">{t.at}</p>
                  <p>{t.event}</p>
                  {t.refs.length > 0 && <CitationGroup refs={t.refs} onSelect={open} />}
                </li>
              ))}
            </ol>
          </Block>
        )}
        <EvidenceViews investigationId={investigation.id} turnId={turnId} onCite={open} />
        <div className="grid gap-4 lg:grid-cols-2">
          {report.gaps.length > 0 && (
            <Block title="Gaps">
              <ul className="list-disc space-y-1 pl-5 text-sm">
                {report.gaps.map((g, i) => (
                  <li key={i}>{g}</li>
                ))}
              </ul>
            </Block>
          )}
          {report.next_checks.length > 0 && (
            <Block title="Next checks">
              <ul className="space-y-1.5 text-sm">
                {report.next_checks.map((n, i) => (
                  <li key={i} className="flex gap-2">
                    <ListChecks
                      aria-hidden
                      className="mt-0.5 size-4 shrink-0 text-muted-foreground"
                    />
                    {n}
                  </li>
                ))}
              </ul>
            </Block>
          )}
        </div>
      </div>
      <aside aria-label="Versions" className="hidden xl:block">
        <div className="sticky top-20">
          <h2 className="mb-2 text-xs font-medium text-muted-foreground">Versions</h2>
          <VersionNav investigation={investigation} version={version} />
        </div>
      </aside>
      <CitationSheet investigationId={investigation.id} />
    </div>
  )
}

export default function ReportPage() {
  const { id, version } = useParams()
  const number = Number(version)
  const valid = isUuid(id) && Number.isInteger(number) && number > 0
  const report = useReport(id ?? '', number, { enabled: valid })
  const investigation = useInvestigation(id ?? '', { enabled: valid })

  const back = (
    <Button asChild variant="ghost" size="sm" className="mb-2 -ml-2">
      <Link to={valid ? `/investigations/${id}` : '/reports'}>
        <ArrowLeft aria-hidden />
        {valid ? 'Investigation' : 'Reports'}
      </Link>
    </Button>
  )
  const missing = (
    <>
      {back}
      <EmptyState
        icon={FileX}
        title="Report not found"
        description="This report version does not exist, or you are not a member of its organization."
      />
    </>
  )

  if (!valid) return missing
  if (report.isPending || investigation.isPending) return <PageSkeleton variant="detail" />
  const error = report.error ?? investigation.error
  if (error) {
    if (error instanceof ApiError && error.kind === 'not_found') return missing
    return (
      <>
        {back}
        <ErrorState
          error={error}
          onRetry={() => {
            void report.refetch()
            void investigation.refetch()
          }}
        />
      </>
    )
  }

  if (!report.data || !investigation.data) return <PageSkeleton variant="detail" />

  const parsed = reportContent.safeParse(report.data.report_json)
  if (!parsed.success) {
    return (
      <>
        {back}
        <EmptyState
          icon={FileX}
          title="This report could not be displayed"
          description="Its stored content does not match any known report format."
        />
      </>
    )
  }
  return (
    <InOrganization organizationId={investigation.data.organization_id}>
      {back}
      <ReportBody
        investigation={investigation.data}
        version={report.data.version}
        createdAt={report.data.created_at}
        turnId={report.data.turn_id}
        report={parsed.data}
      />
    </InOrganization>
  )
}
