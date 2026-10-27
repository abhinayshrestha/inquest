import { FileText, Loader2, Search, SearchX } from 'lucide-react'
import { useId, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { PageHeader } from '@/components/page-header'
import { ListSkeleton } from '@/components/skeletons'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useEnvironments } from '@/features/environments/use-environments'
import { reportOutcome, type ReportListItem, type ReportOutcome } from '@/lib/api'
import { formatDateTime } from '@/lib/format'

import { useReportList } from './queries'
import { OUTCOME_LABELS } from './report-view/vocabulary'

const SELECT =
  'h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 dark:bg-input/30'

function ReportRow({ item }: { item: ReportListItem }) {
  return (
    <li>
      <Link
        to={`/investigations/${item.investigation_id}/reports/${item.version}`}
        className="flex flex-col gap-2 p-4 hover:bg-accent/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset sm:flex-row sm:items-center sm:gap-4"
      >
        <FileText aria-hidden className="hidden size-4 shrink-0 text-muted-foreground sm:block" />
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium">{item.investigation_title}</p>
          <p className="truncate text-xs text-muted-foreground">
            v{item.version} ·{' '}
            {item.turn_seq === 1 ? 'initial question' : `follow-up ${item.turn_seq - 1}`} ·{' '}
            {item.conclusion_count} conclusion{item.conclusion_count === 1 ? '' : 's'} ·{' '}
            {item.environments.map((e) => e.name).join(', ') || 'no environment'} ·{' '}
            {formatDateTime(item.created_at)}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-1.5">
          {item.outcome ? (
            <Badge variant="secondary">{OUTCOME_LABELS[item.outcome]}</Badge>
          ) : (
            <Badge variant="outline">Unknown outcome</Badge>
          )}
          {item.is_latest ? (
            <Badge variant="outline">Latest</Badge>
          ) : (
            <Badge variant="outline" className="text-muted-foreground">
              Superseded
            </Badge>
          )}
        </div>
      </Link>
    </li>
  )
}

export default function ReportsPage() {
  // Filters live in the URL so a filtered list can be reloaded and shared.
  const [params, setParams] = useSearchParams()
  const outcome = reportOutcome.safeParse(params.get('outcome')).data
  const environmentId = params.get('environment') ?? undefined
  const allVersions = params.get('versions') === 'all'
  const q = params.get('q') ?? ''
  const [draft, setDraft] = useState(q)
  const environments = useEnvironments()
  const list = useReportList({
    outcome,
    environmentId,
    latestOnly: !allVersions,
    q: q || undefined,
  })
  const items = list.data?.pages.flatMap((p) => p.items) ?? []
  const ids = { q: useId(), outcome: useId(), environment: useId(), versions: useId() }
  const filtered = Boolean(outcome || environmentId || q)

  function set(key: string, value: string | null) {
    setParams(
      (p) => {
        if (value) p.set(key, value)
        else p.delete(key)
        return p
      },
      { replace: true },
    )
  }

  function search(event: FormEvent) {
    event.preventDefault()
    set('q', draft.trim().slice(0, 100) || null)
  }

  return (
    <>
      <PageHeader
        title="Reports"
        description="Evidence-backed reports across investigations. Each version is a record of what was observed then."
      />
      <div className="mb-4 grid gap-3 rounded-lg border p-3 sm:grid-cols-2 lg:grid-cols-[1fr_12rem_12rem_auto] lg:items-end">
        <form onSubmit={search} role="search" className="space-y-1.5">
          <Label htmlFor={ids.q}>Investigation title</Label>
          <div className="flex gap-2">
            <Input
              id={ids.q}
              value={draft}
              maxLength={100}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="Search titles"
            />
            <Button type="submit" variant="outline" size="icon" aria-label="Search reports">
              <Search aria-hidden />
            </Button>
          </div>
        </form>
        <div className="space-y-1.5">
          <Label htmlFor={ids.outcome}>Outcome</Label>
          <select
            id={ids.outcome}
            className={SELECT}
            value={outcome ?? ''}
            onChange={(e) => set('outcome', e.target.value || null)}
          >
            <option value="">Any outcome</option>
            {(Object.keys(OUTCOME_LABELS) as ReportOutcome[]).map((o) => (
              <option key={o} value={o}>
                {OUTCOME_LABELS[o]}
              </option>
            ))}
          </select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={ids.environment}>Environment</Label>
          <select
            id={ids.environment}
            className={SELECT}
            value={environmentId ?? ''}
            onChange={(e) => set('environment', e.target.value || null)}
          >
            <option value="">Any environment</option>
            {(environments.data ?? []).map((e) => (
              <option key={e.id} value={e.id}>
                {e.name}
              </option>
            ))}
          </select>
        </div>
        <label htmlFor={ids.versions} className="flex h-9 items-center gap-2 text-sm">
          <input
            id={ids.versions}
            type="checkbox"
            checked={allVersions}
            onChange={(e) => set('versions', e.target.checked ? 'all' : null)}
            className="size-4 accent-primary"
          />
          Include earlier versions
        </label>
      </div>

      {list.isPending ? (
        <ListSkeleton />
      ) : list.error ? (
        <ErrorState error={list.error} onRetry={() => list.refetch()} />
      ) : items.length === 0 ? (
        filtered ? (
          <EmptyState
            icon={SearchX}
            title="No reports match these filters"
            description="Change or clear the filters to see more reports."
            action={
              <Button variant="outline" size="sm" asChild>
                <Link to="/reports">Clear filters</Link>
              </Button>
            }
          />
        ) : (
          <EmptyState
            icon={FileText}
            title="No reports yet"
            description="An investigation records a report after its first answer."
          />
        )
      ) : (
        <div className="space-y-3">
          <ul className="divide-y rounded-lg border" aria-label="Reports">
            {items.map((item) => (
              <ReportRow key={item.id} item={item} />
            ))}
          </ul>
          {list.hasNextPage && (
            <div className="flex justify-center">
              <Button
                variant="outline"
                onClick={() => list.fetchNextPage()}
                disabled={list.isFetchingNextPage}
              >
                {list.isFetchingNextPage && <Loader2 aria-hidden className="animate-spin" />}
                Load more
              </Button>
            </div>
          )}
        </div>
      )}
    </>
  )
}
