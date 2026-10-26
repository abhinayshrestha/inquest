import { Bot, UserRound } from 'lucide-react'

import { StatusBadge } from '@/components/status-badge'
import type { TurnRead } from '@/lib/api'
import { formatDateTime } from '@/lib/format'
import type { TurnActivity } from '@/lib/sse/activity'

import { ActivityPanel } from './activity-panel'
import { AnswerView } from './answer-view'

const ACTIVE = new Set(['pending', 'running'])

export function TurnView({
  turn,
  activity,
  investigationId,
  environmentNames,
  onCite,
}: {
  turn: TurnRead
  activity: TurnActivity | undefined
  investigationId: string
  environmentNames: ReadonlyMap<string, string>
  onCite: (ref: string) => void
}) {
  // A pending turn that nobody started (created but not accepted) has no activity yet.
  const live = turn.status === 'running' || (turn.status === 'pending' && Boolean(activity?.queued))
  return (
    <article aria-labelledby={`turn-${turn.id}`} className="space-y-4" data-turn-seq={turn.seq}>
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-primary px-4 py-2.5 text-sm text-primary-foreground">
          <h2 id={`turn-${turn.id}`} className="sr-only">
            {turn.kind === 'initial' ? 'Question' : `Follow-up ${turn.seq - 1}`}
          </h2>
          <p className="whitespace-pre-wrap">{turn.question}</p>
          <p className="mt-1 flex items-center justify-end gap-1 text-[0.7rem] opacity-80">
            <UserRound aria-hidden className="size-3" />
            {formatDateTime(turn.created_at)}
          </p>
        </div>
      </div>
      <div className="flex gap-3">
        <div className="flex size-7 shrink-0 items-center justify-center rounded-full bg-accent text-accent-foreground">
          <Bot aria-hidden className="size-4" />
        </div>
        <div className="min-w-0 flex-1 space-y-3">
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span className="font-medium text-foreground">Inquest</span>
            {ACTIVE.has(turn.status) ? (
              <StatusBadge status={live ? 'running' : turn.status} />
            ) : (
              <span>{formatDateTime(turn.completed_at)}</span>
            )}
          </div>
          <ActivityPanel
            key={live ? 'live' : 'done'}
            activity={activity}
            live={live}
            environmentNames={environmentNames}
            onCite={onCite}
          />
          {turn.status === 'pending' && !live && (
            <p className="text-sm text-muted-foreground">Not started yet.</p>
          )}
          <AnswerView turn={turn} investigationId={investigationId} onCite={onCite} />
        </div>
      </div>
    </article>
  )
}
