import { ArrowLeft, FileX } from 'lucide-react'
import { Link, useParams } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { PageHeader } from '@/components/page-header'
import { PageSkeleton } from '@/components/skeletons'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { ApiError } from '@/lib/api'
import { reportContent, type ReportConclusion } from '@/lib/api/content'
import { formatDateTime, humanize } from '@/lib/format'
import { isUuid } from '@/lib/ids'
import { CitationChip } from '@/lib/render/citation-chip'
import { Markdown } from '@/lib/render/markdown'

import { CitationSheet } from './conversation/citation-sheet'
import { useCitationParam } from './conversation/use-citation-param'
import { useReport } from './queries'

// A basic, read-only view of one report version: outcome, summary, conclusions with
// citations, gaps and next checks. The full report renderer is a later phase.

function Refs({ refs, onCite }: { refs: string[]; onCite: (ref: string) => void }) {
  if (refs.length === 0) return null
  return (
    <span className="inline-flex flex-wrap gap-1">
      {refs.map((r) => (
        <CitationChip key={r} citationRef={r} onSelect={onCite} />
      ))}
    </span>
  )
}

function ConclusionItem({ c, onCite }: { c: ReportConclusion; onCite: (ref: string) => void }) {
  return (
    <li className="space-y-1.5 rounded-md border p-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant="secondary">{humanize(c.status)}</Badge>
        {c.confidence && <Badge variant="outline">{humanize(c.confidence)} confidence</Badge>}
        {c.claim && c.claim !== 'observation' && (
          <Badge variant="outline">{humanize(c.claim)}</Badge>
        )}
      </div>
      <p className="text-sm">{c.statement}</p>
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        {c.supporting.length > 0 && (
          <>
            Supporting <Refs refs={c.supporting} onCite={onCite} />
          </>
        )}
        {c.contradicting.length > 0 && (
          <>
            Contradicting <Refs refs={c.contradicting} onCite={onCite} />
          </>
        )}
      </div>
    </li>
  )
}

function List({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <ul className="list-disc space-y-1 pl-5 text-sm">
          {items.map((item, i) => (
            <li key={i}>{item}</li>
          ))}
        </ul>
      </CardContent>
    </Card>
  )
}

export default function ReportPage() {
  const { id, version } = useParams()
  const number = Number(version)
  const valid = isUuid(id) && Number.isInteger(number) && number > 0
  const query = useReport(id ?? '', number, { enabled: valid })
  const { open } = useCitationParam()

  const back = (
    <Button asChild variant="ghost" size="sm" className="mb-2 -ml-2">
      <Link to={valid ? `/investigations/${id}` : '/investigations'}>
        <ArrowLeft aria-hidden />
        Investigation
      </Link>
    </Button>
  )
  const missing = (
    <>
      {back}
      <EmptyState
        icon={FileX}
        title="Report not found"
        description="This report version does not exist."
      />
    </>
  )

  if (!valid) return missing
  if (query.isPending) return <PageSkeleton variant="detail" />
  if (query.error) {
    if (query.error instanceof ApiError && query.error.kind === 'not_found') return missing
    return (
      <>
        {back}
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
      </>
    )
  }

  const parsed = reportContent.safeParse(query.data.report_json)
  return (
    <>
      {back}
      <PageHeader
        title={`Report v${query.data.version}`}
        description={`Recorded ${formatDateTime(query.data.created_at)}. Versions are immutable.`}
      />
      {!parsed.success ? (
        <EmptyState
          icon={FileX}
          title="This report could not be displayed"
          description="Its content does not match the expected report format."
        />
      ) : (
        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                Summary <Badge variant="secondary">{humanize(parsed.data.outcome)}</Badge>
              </CardTitle>
            </CardHeader>
            <CardContent>
              <Markdown onCite={open}>{parsed.data.summary}</Markdown>
            </CardContent>
          </Card>
          {parsed.data.conclusions.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Conclusions</CardTitle>
              </CardHeader>
              <CardContent>
                <ul className="space-y-2">
                  {parsed.data.conclusions.map((c, i) => (
                    <ConclusionItem key={c.key ?? i} c={c} onCite={open} />
                  ))}
                </ul>
              </CardContent>
            </Card>
          )}
          <List title="Gaps" items={parsed.data.gaps} />
          <List title="Next checks" items={parsed.data.next_checks} />
          <p className="text-xs text-muted-foreground">
            A richer report view (timeline, version diffs) comes in a later phase.
          </p>
        </div>
      )}
      <CitationSheet investigationId={id} />
    </>
  )
}
