import { useQueryClient } from '@tanstack/react-query'
import { ArrowDown, ArrowLeft, ChevronDown, Loader2, Play, SearchX } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { PageHeader } from '@/components/page-header'
import { PageSkeleton } from '@/components/skeletons'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { ApiError, queryKeys, type InvestigationRead, type ProgressEvent } from '@/lib/api'
import { isUuid } from '@/lib/ids'
import { buildActivity } from '@/lib/sse/activity'
import { REFRESH_EVENTS } from '@/lib/sse/events'
import { useEventStream } from '@/lib/sse/use-event-stream'
import { SESSION_KEY } from '@/app/contexts/auth'
import { ME_KEY } from '@/app/contexts/organization'
import { InOrganization } from '@/features/organizations/organization-gate'
import { cn } from 'cn'

import { CitationSheet } from './conversation/citation-sheet'
import { useCitationParam } from './conversation/use-citation-param'
import { Composer } from './conversation/composer'
import { InvestigationDetails } from './conversation/investigation-aside'
import { TurnView } from './conversation/turn-view'
import { useStickToBottom } from './conversation/use-stick-to-bottom'
import { useAskFollowUp, useCancelTurn, useInvestigation, useStartInvestigation } from './queries'

const TERMINAL = new Set(['completed', 'failed', 'cancelled'])
// Replays after (re)connecting deliver bursts of events; one refetch per burst.
const REFRESH_DEBOUNCE_MS = 250

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
        description="It does not exist, or it belongs to an organization you are not a member of."
      />
    </>
  )
}

function useRefreshOnEvents(id: string) {
  const client = useQueryClient()
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(timer.current), [])
  return useCallback(
    (event: ProgressEvent) => {
      if (!REFRESH_EVENTS.has(event.type)) return
      clearTimeout(timer.current)
      timer.current = setTimeout(() => {
        void client.invalidateQueries({ queryKey: queryKeys.investigation(id) })
        void client.invalidateQueries({ queryKey: queryKeys.investigations })
      }, REFRESH_DEBOUNCE_MS)
    },
    [client, id],
  )
}

function composerState(investigation: InvestigationRead, hasActiveTurn: boolean) {
  if (TERMINAL.has(investigation.status)) return 'This investigation is closed.'
  if (hasActiveTurn) return 'Inquest is working on the current question.'
  if (investigation.status === 'pending') return 'Start the investigation to ask follow-ups.'
  if (investigation.status !== 'idle') return 'Waiting for the investigation to become idle.'
  return null
}

function Conversation({ investigation }: { investigation: InvestigationRead }) {
  const id = investigation.id
  const location = useLocation()
  const onEvent = useRefreshOnEvents(id)
  const stream = useEventStream(id, onEvent)
  const client = useQueryClient()
  // Access ended while watching: re-read the investigation (now not found) and the
  // memberships, so the page and the organization switcher show the new state.
  useEffect(() => {
    if (stream.status !== 'revoked') return
    void client.invalidateQueries({ queryKey: queryKeys.investigation(id) })
    void client.invalidateQueries({ queryKey: ME_KEY })
    void client.invalidateQueries({ queryKey: SESSION_KEY })
  }, [client, id, stream.status])
  const activity = useMemo(() => buildActivity(stream.events), [stream.events])
  const { open: openCitation } = useCitationParam()
  const start = useStartInvestigation(id)
  const ask = useAskFollowUp(id)
  const cancel = useCancelTurn(id)
  const [detailsOpen, setDetailsOpen] = useState(false)

  const turns = useMemo(
    () => [...investigation.turns].sort((a, b) => a.seq - b.seq),
    [investigation.turns],
  )
  const environmentNames = useMemo(
    () => new Map(investigation.assignments.map((a) => [a.environment.id, a.environment.name])),
    [investigation.assignments],
  )
  const environments = [...new Set(environmentNames.values())]
  const activeTurn = turns.find((t) => t.status === 'pending' || t.status === 'running')
  const activeQueued = activeTurn ? Boolean(activity.get(activeTurn.id)?.queued) : false
  const startable =
    investigation.status === 'pending' &&
    activeTurn?.seq === 1 &&
    activeTurn.status === 'pending' &&
    !activeQueued &&
    !start.isSuccess
  const canCancel =
    activeTurn !== undefined &&
    (activeTurn.status === 'running' || activeQueued || start.isSuccess || ask.isSuccess)
  const startError = (location.state as { startError?: string } | null)?.startError

  const { unseen, jump } = useStickToBottom(
    `${turns.map((t) => t.status).join()}:${stream.events.length}`,
    true,
  )

  const details = (
    <InvestigationDetails
      investigation={investigation}
      environments={environments}
      stream={stream.status}
    />
  )

  return (
    <div className="xl:grid xl:grid-cols-[minmax(0,1fr)_15rem] xl:gap-8">
      {/* At least one viewport tall (minus header and main's top padding) so the composer,
          pushed down by mt-auto, sits at the bottom even for a short conversation. */}
      <div className="flex min-h-[calc(100dvh-5rem)] min-w-0 flex-col">
        <BackLink />
        <PageHeader
          title={investigation.title}
          description={investigation.question}
          actions={
            startable && (
              <Button onClick={() => start.mutate()} disabled={start.isPending}>
                {start.isPending ? (
                  <Loader2 aria-hidden className="animate-spin" />
                ) : (
                  <Play aria-hidden />
                )}
                Start investigation
              </Button>
            )
          }
        />
        <Collapsible
          open={detailsOpen}
          onOpenChange={setDetailsOpen}
          className="mb-6 rounded-lg border xl:hidden"
        >
          <CollapsibleTrigger className="flex w-full items-center justify-between px-4 py-2.5 text-sm font-medium focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
            Details and reports
            <ChevronDown
              aria-hidden
              className={cn('size-4 transition-transform', detailsOpen && 'rotate-180')}
            />
          </CollapsibleTrigger>
          <CollapsibleContent className="border-t px-4 py-3">{details}</CollapsibleContent>
        </Collapsible>

        <div className="space-y-3">
          {startable && startError && !start.error && (
            <ErrorState
              title="Created, but not started"
              error={new ApiError('unavailable', 503, startError)}
            />
          )}
          {start.error && (
            <ErrorState
              title="Could not start"
              error={start.error}
              onRetry={() => start.mutate()}
            />
          )}
          {cancel.error && <ErrorState title="Could not cancel" error={cancel.error} />}
        </div>

        <section aria-label="Conversation" className="space-y-8 pb-6">
          {turns.map((turn) => (
            <TurnView
              key={turn.id}
              turn={turn}
              activity={activity.get(turn.id)}
              investigationId={id}
              environmentNames={environmentNames}
              onCite={openCitation}
            />
          ))}
        </section>

        {unseen && (
          <Button
            size="sm"
            variant="secondary"
            className="fixed right-4 bottom-28 z-20 shadow-md sm:right-8"
            onClick={() => jump()}
          >
            <ArrowDown aria-hidden />
            Jump to latest
          </Button>
        )}

        <Composer
          disabledReason={composerState(investigation, activeTurn !== undefined)}
          submitting={ask.isPending}
          submitError={ask.error}
          onSubmit={(question) => ask.mutateAsync(question)}
          canCancel={canCancel}
          cancelling={cancel.isPending}
          onCancel={() => cancel.mutate()}
        />
      </div>
      <aside aria-label="Investigation details" className="hidden xl:block">
        <div className="sticky top-20">{details}</div>
      </aside>
      <CitationSheet investigationId={id} />
    </div>
  )
}

function InvestigationView({ id }: { id: string }) {
  const query = useInvestigation(id)
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
  // The live stream lives inside: switching organization unmounts it and closes it.
  return (
    <InOrganization organizationId={query.data.organization_id}>
      <Conversation investigation={query.data} />
    </InOrganization>
  )
}

export default function InvestigationDetailPage() {
  const { id } = useParams()
  if (!isUuid(id)) return <NotFound />
  // Keyed so live state (events, open panels) never carries over between investigations.
  return <InvestigationView key={id} id={id} />
}
