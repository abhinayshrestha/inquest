import { Loader2, Plus, Search } from 'lucide-react'
import { Link } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { PageHeader } from '@/components/page-header'
import { ListSkeleton } from '@/components/skeletons'
import { Button } from '@/components/ui/button'

import { InvestigationRow } from './investigation-row'
import { useInvestigationList } from './queries'

export default function InvestigationsPage() {
  const list = useInvestigationList()
  const items = list.data?.pages.flatMap((p) => p.items) ?? []
  const newButton = (
    <Button asChild>
      <Link to="/investigations/new">
        <Plus aria-hidden />
        New investigation
      </Link>
    </Button>
  )

  return (
    <>
      <PageHeader
        title="Investigations"
        description="Conversational investigations: questions, evidence and evolving reports."
        actions={newButton}
      />
      {list.isPending ? (
        <ListSkeleton />
      ) : list.error ? (
        <ErrorState error={list.error} onRetry={() => list.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          icon={Search}
          title="No investigations yet"
          description="Ask Inquest why something is failing or slow; it gathers evidence and answers with citations."
          action={newButton}
        />
      ) : (
        <div className="space-y-3">
          <ul className="divide-y rounded-lg border" aria-label="Investigation history">
            {items.map((item) => (
              <InvestigationRow key={item.id} item={item} />
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
