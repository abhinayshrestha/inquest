import { ArrowUp, Loader2, Square } from 'lucide-react'
import { useId, useState, type FormEvent, type KeyboardEvent } from 'react'

import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { describeError } from '@/lib/errors'

const MAX_QUESTION = 10_000

export function Composer({
  disabledReason,
  submitting,
  submitError,
  onSubmit,
  canCancel,
  cancelling,
  onCancel,
}: {
  // Why a follow-up cannot be asked right now; null when it can.
  disabledReason: string | null
  submitting: boolean
  submitError: unknown
  onSubmit: (question: string) => Promise<unknown>
  canCancel: boolean
  cancelling: boolean
  onCancel: () => void
}) {
  const [question, setQuestion] = useState('')
  const inputId = useId()
  const hintId = useId()
  const trimmed = question.trim()
  const disabled = disabledReason !== null || submitting

  async function submit(event?: FormEvent) {
    event?.preventDefault()
    if (disabled || !trimmed) return
    try {
      await onSubmit(trimmed)
      setQuestion('')
    } catch {
      // Shown through submitError; the question stays so it can be retried.
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) void submit()
  }

  // Pinned to the bottom of the viewport: mt-auto places it at the end of a short
  // conversation column, sticky keeps it there while scrolling, and -mb-6 cancels the
  // main element's bottom padding so it sits flush with the screen edge.
  return (
    <form
      onSubmit={submit}
      className="sticky bottom-0 z-10 -mx-4 mt-auto -mb-6 border-t bg-background px-4 pt-3 pb-[max(1rem,env(safe-area-inset-bottom))] sm:mx-0 sm:rounded-t-lg sm:border-x sm:px-3"
    >
      <label htmlFor={inputId} className="sr-only">
        Follow-up question
      </label>
      <div className="flex items-end gap-2">
        <Textarea
          id={inputId}
          value={question}
          onChange={(e) => setQuestion(e.target.value.slice(0, MAX_QUESTION))}
          onKeyDown={onKeyDown}
          placeholder={disabledReason ?? 'Ask a follow-up question…'}
          disabled={disabledReason !== null}
          rows={2}
          className="max-h-48 min-h-11 resize-y"
          aria-describedby={hintId}
        />
        {canCancel ? (
          <Button
            type="button"
            variant="outline"
            onClick={onCancel}
            disabled={cancelling}
            aria-label="Cancel the running turn"
          >
            {cancelling ? <Loader2 aria-hidden className="animate-spin" /> : <Square aria-hidden />}
            <span className="hidden sm:inline">Cancel</span>
          </Button>
        ) : (
          <Button type="submit" disabled={disabled || !trimmed} aria-label="Send follow-up">
            {submitting ? (
              <Loader2 aria-hidden className="animate-spin" />
            ) : (
              <ArrowUp aria-hidden />
            )}
            <span className="hidden sm:inline">Ask</span>
          </Button>
        )}
      </div>
      <p id={hintId} className="mt-1.5 text-xs text-muted-foreground" aria-live="polite">
        {submitError ? (
          <span className="text-destructive">{describeError(submitError).description}</span>
        ) : disabledReason ? (
          disabledReason
        ) : (
          'Inquest reuses existing evidence when it can. Ctrl/⌘ + Enter to send.'
        )}
      </p>
    </form>
  )
}
