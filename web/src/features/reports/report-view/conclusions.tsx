import { Clock, History } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import type { Claim, HistoricalConclusion, ReportConclusion } from '@/lib/api'
import { ago, formatUtc, parseTime } from '@/lib/freshness'
import { humanize } from '@/lib/format'
import { CitationGroup } from '@/lib/render/citation-group'
import { cn } from 'cn'

import { CLAIM_INFO, CLAIM_ORDER, STATUS_TONE } from './vocabulary'

type OnCite = (ref: string) => void

const TONE_CLASS = {
  success: 'border-success/30 bg-success/10 text-success',
  info: 'border-info/30 bg-info/10 text-info',
  warning: 'border-warning/30 bg-warning/10 text-warning',
  danger: 'border-destructive/30 bg-destructive/10 text-destructive',
  neutral: 'border-transparent bg-muted text-muted-foreground',
}

export function StatusPill({ status }: { status: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-md border px-1.5 py-0.5 text-xs font-medium',
        TONE_CLASS[STATUS_TONE[status] ?? 'neutral'],
      )}
    >
      {humanize(status)}
    </span>
  )
}

// When the evidence behind a statement was observed, always with its age: a report is a
// record of that time, never a reading of the present.
export function ObservedAt({ asOf }: { asOf: string | null | undefined }) {
  const t = parseTime(asOf)
  if (t === null)
    return <span className="text-xs text-muted-foreground">Observation time not recorded</span>
  return (
    <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
      <Clock aria-hidden className="size-3" />
      Observed up to {formatUtc(t)} ({ago(t)})
    </span>
  )
}

function Citations({ label, refs, onCite }: { label: string; refs: string[]; onCite: OnCite }) {
  if (refs.length === 0) return null
  return (
    <span className="inline-flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
      {label} <CitationGroup refs={refs} onSelect={onCite} />
    </span>
  )
}

function ConclusionCard({ c, onCite }: { c: ReportConclusion; onCite: OnCite }) {
  return (
    <li className="space-y-2 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <StatusPill status={c.status} />
        {c.confidence && <Badge variant="outline">{humanize(c.confidence)} confidence</Badge>}
      </div>
      <p className="text-sm">{c.statement}</p>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <ObservedAt asOf={c.as_of} />
        <Citations label="Supported by" refs={c.supporting} onCite={onCite} />
        <Citations label="Contradicted by" refs={c.contradicting} onCite={onCite} />
      </div>
    </li>
  )
}

// Current conclusions, grouped by what they claim (causal first) with the meaning of each
// level stated, so observations, co-occurrences, correlations and causes stay distinct.
export function CurrentConclusions({
  conclusions,
  onCite,
}: {
  conclusions: ReportConclusion[]
  onCite: OnCite
}) {
  if (conclusions.length === 0) {
    return <p className="text-sm text-muted-foreground">This version has no current conclusions.</p>
  }
  const groups = new Map<Claim | 'unclassified', ReportConclusion[]>()
  for (const c of conclusions) {
    const claim = c.claim ?? 'unclassified'
    groups.set(claim, [...(groups.get(claim) ?? []), c])
  }
  return (
    <div className="space-y-5">
      {CLAIM_ORDER.filter((claim) => groups.has(claim)).map((claim) => (
        <section key={claim} aria-labelledby={`claim-${claim}`} className="space-y-2">
          <div>
            <h3 id={`claim-${claim}`} className="text-sm font-semibold">
              {CLAIM_INFO[claim].label}{' '}
              <span className="font-normal text-muted-foreground">
                ({groups.get(claim)!.length})
              </span>
            </h3>
            <p className="text-xs text-muted-foreground">{CLAIM_INFO[claim].meaning}</p>
          </div>
          <ul className="space-y-2">
            {groups.get(claim)!.map((c, i) => (
              <ConclusionCard key={c.key ?? i} c={c} onCite={onCite} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}

// Conclusions earlier versions held that this one no longer restates. Kept for the record,
// marked with the version they came from and when they were observed.
export function HistoricalConclusions({
  history,
  onCite,
}: {
  history: HistoricalConclusion[]
  onCite: OnCite
}) {
  if (history.length === 0) return null
  return (
    <ul className="space-y-2" aria-label="Historical conclusions">
      {history.map((h, i) => {
        const retracted = h.status === 'retracted'
        return (
          <li key={h.key ?? i} className="space-y-1.5 rounded-lg border border-dashed p-3">
            <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
              <History aria-hidden className="size-3.5" />
              {h.from_version ? `From version ${h.from_version}` : 'From an earlier version'}
              <StatusPill status={h.status} />
            </div>
            <p className={cn('text-sm text-muted-foreground', retracted && 'line-through')}>
              {h.statement}
            </p>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
              <ObservedAt asOf={h.as_of} />
              <Citations label="Supported by" refs={h.supporting} onCite={onCite} />
            </div>
          </li>
        )
      })}
    </ul>
  )
}
