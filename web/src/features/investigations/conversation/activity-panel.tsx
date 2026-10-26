import {
  AlertTriangle,
  Check,
  ChevronRight,
  CircleDashed,
  Lightbulb,
  ListChecks,
  Loader2,
  Minus,
  Server,
  Sparkles,
  X,
} from 'lucide-react'
import { useState } from 'react'

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { CitationChip } from '@/lib/render/citation-chip'
import {
  activitySummary,
  type ActivityStep,
  type AssignmentItem,
  type AssignmentStep,
  type ToolCallItem,
  type TurnActivity,
} from '@/lib/sse/activity'
import { humanize } from '@/lib/format'
import { cn } from 'cn'

import {
  DECISION_LABELS,
  TELEMETRY_LABELS,
  toolLabel,
  toolSource,
  toolStatusLabel,
} from '../labels'

// Everything here comes from deterministic progress events (D024): tool names, statuses,
// allowlisted query scope, refs and recorded hypothesis/finding text. No model reasoning,
// no tool output.

function duration(start: string, end: string | null) {
  if (!end) return null
  const ms = new Date(end).getTime() - new Date(start).getTime()
  if (!Number.isFinite(ms) || ms < 0) return null
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`
}

function ToolIcon({ item }: { item: ToolCallItem }) {
  if (item.status === 'running')
    return <Loader2 aria-hidden className="size-3.5 animate-spin text-info" />
  if (item.status === 'succeeded')
    return item.outcome === 'observations' || item.outcome === null ? (
      <Check aria-hidden className="size-3.5 text-success" />
    ) : (
      <Minus aria-hidden className="size-3.5 text-muted-foreground" />
    )
  if (item.status === 'rejected' || item.status === 'cancelled')
    return <AlertTriangle aria-hidden className="size-3.5 text-warning" />
  return <X aria-hidden className="size-3.5 text-destructive" />
}

function ToolRow({ item, onCite }: { item: ToolCallItem; onCite?: (ref: string) => void }) {
  const took = duration(item.startedAt, item.finishedAt)
  const source = toolSource(item.tool)
  return (
    <li className="space-y-1">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        <ToolIcon item={item} />
        <span className="font-medium">{toolLabel(item.tool)}</span>
        {source && <span className="text-xs text-muted-foreground">{source}</span>}
        <span
          className={cn(
            'text-xs',
            item.status === 'succeeded' || item.status === 'running'
              ? 'text-muted-foreground'
              : 'text-destructive',
          )}
        >
          {toolStatusLabel(item.status, item.outcome)}
          {took && ` · ${took}`}
          {item.replayed && ' · replayed'}
        </span>
      </div>
      {(item.scope.length > 0 || item.errorCode || item.telemetryAvailable) && (
        <div className="ml-5.5 flex flex-wrap gap-1.5 text-xs text-muted-foreground">
          {item.scope.map((s) => (
            <span key={`${s.label}:${s.value}`} className="rounded bg-muted px-1.5 py-0.5">
              {s.label}: <span className="font-mono">{s.value}</span>
            </span>
          ))}
          {item.errorCode && (
            <span className="rounded bg-destructive/10 px-1.5 py-0.5 font-mono text-destructive">
              {item.errorCode}
            </span>
          )}
          {item.telemetryAvailable && item.outcome !== 'observations' && (
            <span>{TELEMETRY_LABELS[item.telemetryAvailable] ?? item.telemetryAvailable}</span>
          )}
        </div>
      )}
      {item.evidence.length > 0 && (
        <div className="ml-5.5 flex flex-wrap items-center gap-1 text-xs">
          <span className="text-muted-foreground">Evidence</span>
          {item.evidence.map((ref) => (
            <CitationChip key={ref} citationRef={ref} onSelect={onCite} />
          ))}
        </div>
      )}
    </li>
  )
}

function ItemRow({ item, onCite }: { item: AssignmentItem; onCite?: (ref: string) => void }) {
  if (item.kind === 'tool') return <ToolRow item={item} onCite={onCite} />
  const Icon = item.kind === 'hypothesis' ? Lightbulb : ListChecks
  const text = item.kind === 'hypothesis' ? item.statement : item.title
  return (
    <li className="flex items-start gap-2 text-sm">
      <Icon aria-hidden className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
      <div className="min-w-0 space-x-1.5">
        <span className="text-xs text-muted-foreground">
          {item.kind === 'hypothesis' ? 'Hypothesis' : 'Finding'}
          {item.status && ` · ${humanize(item.status)}`}
        </span>
        {text && <span>{text}</span>}
        <CitationChip citationRef={item.ref} onSelect={onCite} />
      </div>
    </li>
  )
}

function AssignmentBlock({
  step,
  environmentName,
  defaultOpen,
  onCite,
}: {
  step: AssignmentStep
  environmentName: string | null
  defaultOpen: boolean
  onCite?: (ref: string) => void
}) {
  const [open, setOpen] = useState(defaultOpen)
  const running = step.status === 'running'
  const tools = step.items.filter((i) => i.kind === 'tool').length
  return (
    <Collapsible open={open} onOpenChange={setOpen} asChild>
      <li className="rounded-md border bg-card">
        <CollapsibleTrigger className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm hover:bg-accent/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
          <ChevronRight
            aria-hidden
            className={cn('size-4 shrink-0 transition-transform', open && 'rotate-90')}
          />
          {running ? (
            <Loader2 aria-hidden className="size-4 shrink-0 animate-spin text-info" />
          ) : (
            <Server aria-hidden className="size-4 shrink-0 text-muted-foreground" />
          )}
          <span className="min-w-0 flex-1 truncate">
            <span className="font-medium">Investigating {environmentName ?? 'environment'}</span>
            {step.round !== null && (
              <span className="text-muted-foreground"> · round {step.round}</span>
            )}
          </span>
          <span className="shrink-0 text-xs text-muted-foreground">
            {tools} tool call{tools === 1 ? '' : 's'}
            {!running && ` · ${humanize(step.status)}`}
          </span>
        </CollapsibleTrigger>
        <CollapsibleContent className="space-y-2 border-t px-3 py-2">
          {step.objective && (
            <p className="text-xs text-muted-foreground">
              <span className="font-medium">Objective:</span> {step.objective}
            </p>
          )}
          {step.items.length === 0 ? (
            <p className="text-xs text-muted-foreground">Preparing hypotheses…</p>
          ) : (
            <ul className="space-y-2">
              {step.items.map((item) => (
                <ItemRow key={`${item.kind}:${item.key}`} item={item} onCite={onCite} />
              ))}
            </ul>
          )}
          {step.budget && (
            <p className="flex items-center gap-1.5 text-xs text-warning">
              <AlertTriangle aria-hidden className="size-3.5" />
              Budget reached ({humanize(step.budget)}); conclusions drawn from what was gathered.
            </p>
          )}
        </CollapsibleContent>
      </li>
    </Collapsible>
  )
}

function StepRow({
  step,
  environmentNames,
  live,
  onCite,
}: {
  step: ActivityStep
  environmentNames: ReadonlyMap<string, string>
  live: boolean
  onCite?: (ref: string) => void
}) {
  if (step.kind === 'assignment') {
    return (
      <AssignmentBlock
        step={step}
        environmentName={
          step.environmentId ? (environmentNames.get(step.environmentId) ?? null) : null
        }
        defaultOpen={live}
        onCite={onCite}
      />
    )
  }
  if (step.kind === 'decision') {
    return (
      <li className="flex items-start gap-2 text-sm">
        <Sparkles aria-hidden className="mt-0.5 size-4 shrink-0 text-primary" />
        <div className="min-w-0">
          <p className="font-medium">{DECISION_LABELS[step.decision] ?? humanize(step.decision)}</p>
          {step.objectives.length > 0 && (
            <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-muted-foreground">
              {step.objectives.map((o, i) => (
                <li key={i}>{o}</li>
              ))}
            </ul>
          )}
          {step.limited && (
            <p className="mt-1 text-xs text-warning">
              Round limit reached; answering with what exists.
            </p>
          )}
        </div>
      </li>
    )
  }
  const tones = {
    neutral: 'text-muted-foreground',
    success: 'text-success',
    warning: 'text-warning',
    danger: 'text-destructive',
  }
  return (
    <li className={cn('flex items-center gap-2 text-xs', tones[step.tone])}>
      <CircleDashed aria-hidden className="size-3.5 shrink-0" />
      {step.label}
    </li>
  )
}

export function ActivityPanel({
  activity,
  live,
  environmentNames,
  onCite,
}: {
  activity: TurnActivity | undefined
  live: boolean
  environmentNames: ReadonlyMap<string, string>
  onCite?: (ref: string) => void
}) {
  // Live turns start expanded; finished ones collapse to a summary line.
  const [open, setOpen] = useState(live)
  if (!activity || activity.steps.length === 0) {
    if (!live) return null
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
        <Loader2 aria-hidden className="size-4 animate-spin" />
        Waiting for the investigator…
      </div>
    )
  }
  return (
    <Collapsible open={open || live} onOpenChange={setOpen}>
      <CollapsibleTrigger
        disabled={live}
        className="flex items-center gap-2 rounded text-xs font-medium text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:cursor-default"
      >
        {live ? (
          <Loader2 aria-hidden className="size-3.5 animate-spin text-info" />
        ) : (
          <ChevronRight
            aria-hidden
            className={cn('size-3.5 transition-transform', open && 'rotate-90')}
          />
        )}
        {live ? 'Investigating' : 'Activity'} · {activitySummary(activity)}
      </CollapsibleTrigger>
      <CollapsibleContent>
        <ol className="mt-2 space-y-2 border-l pl-3" aria-label="Investigation activity">
          {activity.steps.map((step) => (
            <StepRow
              key={step.key}
              step={step}
              environmentNames={environmentNames}
              live={live}
              onCite={onCite}
            />
          ))}
        </ol>
      </CollapsibleContent>
    </Collapsible>
  )
}
