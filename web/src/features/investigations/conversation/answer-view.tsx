import type { ReactNode } from 'react'
import { AlertTriangle, Ban, FileText, Info, XCircle } from 'lucide-react'
import { Link } from 'react-router'

import { Badge } from '@/components/ui/badge'
import { answerContent, type TurnRead } from '@/lib/api'
import { ResponseBlocks } from '@/lib/render/blocks/response-blocks'
import { Markdown } from '@/lib/render/markdown'

import { TURN_DECISION_LABELS, turnErrorMessage } from '../labels'
import { usePresentation } from '../queries'

function Note({ icon: Icon, children }: { icon: typeof Info; children: ReactNode }) {
  return (
    <p className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-xs">
      <Icon aria-hidden className="mt-0.5 size-3.5 shrink-0 text-warning" />
      <span>{children}</span>
    </p>
  )
}

// The answer as response blocks (D023), stored server-side and fetched once. The validated
// Markdown answer is shown while blocks load and whenever they cannot be used (request
// failed, unknown version), so an answer is never lost to a presentation problem.
function AnswerBody({
  investigationId,
  turnId,
  answerMd,
  onCite,
}: {
  investigationId: string
  turnId: string
  answerMd: string
  onCite: (ref: string) => void
}) {
  const presentation = usePresentation(investigationId, turnId)
  if (presentation.isSuccess) {
    return <ResponseBlocks blocks={presentation.data.blocks} onCite={onCite} />
  }
  return (
    <div className="space-y-3">
      <Markdown onCite={onCite}>{answerMd}</Markdown>
      {presentation.isPending && (
        <p role="status" className="text-xs text-muted-foreground">
          Preparing evidence views…
        </p>
      )}
    </div>
  )
}

export function AnswerView({
  turn,
  investigationId,
  onCite,
}: {
  turn: TurnRead
  investigationId: string
  onCite: (ref: string) => void
}) {
  if (turn.status === 'failed' || turn.status === 'cancelled') {
    const cancelled = turn.status === 'cancelled'
    const Icon = cancelled ? Ban : XCircle
    return (
      <div
        className="flex items-start gap-2 rounded-md border bg-muted/40 px-3 py-2 text-sm"
        role="status"
      >
        <Icon
          aria-hidden
          className={cancelled ? 'mt-0.5 size-4 text-warning' : 'mt-0.5 size-4 text-destructive'}
        />
        <div>
          <p className="font-medium">{cancelled ? 'Cancelled' : 'This turn failed'}</p>
          <p className="text-muted-foreground">
            {cancelled
              ? 'Stopped before an answer was produced.'
              : turnErrorMessage(turn.error_code)}{' '}
            Earlier answers, evidence and reports are kept; ask again to continue.
          </p>
        </div>
      </div>
    )
  }
  if (turn.status !== 'completed' || !turn.answer_json) return null

  const parsed = answerContent.safeParse(turn.answer_json)
  if (!parsed.success) {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <AlertTriangle aria-hidden className="size-4" />
        This answer could not be displayed.
      </p>
    )
  }
  const answer = parsed.data
  const version = answer.report_version ?? null
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5">
        {turn.decision && (
          <Badge variant="secondary">{TURN_DECISION_LABELS[turn.decision] ?? turn.decision}</Badge>
        )}
        {answer.fallback && <Badge variant="outline">Inquest fallback answer</Badge>}
        {answer.window && (
          // Windows are long ISO ranges; Badge is nowrap by default and would widen the
          // page on phones.
          <Badge variant="outline" className="h-auto max-w-full text-left whitespace-normal">
            Window: {answer.window}
          </Badge>
        )}
        {version !== null && (
          <Badge asChild variant="outline">
            <Link to={`/investigations/${investigationId}/reports/${version}`}>
              <FileText aria-hidden />
              Report v{version}
            </Link>
          </Badge>
        )}
      </div>
      {answer.fallback && (
        <Note icon={Info}>
          The model&apos;s answer did not pass evidence validation, so Inquest lists the validated
          findings instead of a synthesis.
        </Note>
      )}
      {answer.limited && (
        <Note icon={AlertTriangle}>
          The investigation budget was reached; this answer may not cover every part of the
          question.
        </Note>
      )}
      {answer.report_rejected.length > 0 && (
        <Note icon={AlertTriangle}>
          A proposed report revision was not accepted by validation
          {answer.current_report_version ? `; report v${answer.current_report_version} stands` : ''}
          .
        </Note>
      )}
      <AnswerBody
        investigationId={investigationId}
        turnId={turn.id}
        answerMd={answer.answer_md}
        onCite={onCite}
      />
    </div>
  )
}
