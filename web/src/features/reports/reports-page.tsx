import { FileText, Loader2 } from 'lucide-react'
import { Link } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { PageHeader } from '@/components/page-header'
import { ListSkeleton } from '@/components/skeletons'
import { Button } from '@/components/ui/button'
import { useInvestigationList } from '@/features/investigations/queries'
import { formatDateTime } from '@/lib/format'

// Latest report version per investigation, from the investigation history. Investigations
// without a report are skipped; older versions are linked from the investigation.
export default function ReportsPage() {
  const list = useInvestigationList()
  const items = (list.data?.pages.flatMap((p) => p.items) ?? []).filter(
    (i) => i.latest_report_version !== null,
  )
  return (
    <>
      <PageHeader
        title="Reports"
        description="Evidence-backed investigation reports, latest version per investigation."
      />
      {list.isPending ? (
        <ListSkeleton />
      ) : list.error ? (
        <ErrorState error={list.error} onRetry={() => list.refetch()} />
      ) : (
        <div className="space-y-3">
          {items.length === 0 ? (
            <EmptyState
              icon={FileText}
              title="No reports in the loaded investigations"
              description="An investigation records a report after its first answer."
            />
          ) : (
            <ul className="divide-y rounded-lg border" aria-label="Reports">
              {items.map((i) => (
                <li key={i.id}>
                  <Link
                    to={`/investigations/${i.id}/reports/${i.latest_report_version}`}
                    className="flex items-center gap-3 p-4 hover:bg-accent/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset"
                  >
                    <FileText aria-hidden className="size-4 shrink-0 text-muted-foreground" />
                    <div className="min-w-0 flex-1">
                      <p className="truncate font-medium">{i.title}</p>
                      <p className="text-xs text-muted-foreground">
                        Report v{i.latest_report_version} · updated {formatDateTime(i.updated_at)}
                      </p>
                    </div>
                  </Link>
                </li>
              ))}
            </ul>
          )}
          {list.hasNextPage && (
            <div className="flex justify-center">
              <Button
                variant="outline"
                onClick={() => list.fetchNextPage()}
                disabled={list.isFetchingNextPage}
              >
                {list.isFetchingNextPage && <Loader2 aria-hidden className="animate-spin" />}
                Load more investigations
              </Button>
            </div>
          )}
        </div>
      )}
    </>
  )
}
