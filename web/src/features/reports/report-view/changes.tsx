import { ArrowRight } from 'lucide-react'
import type { ReactNode } from 'react'

import type { ReportContent } from '@/lib/api'
import { humanize } from '@/lib/format'
import { CitationGroup } from '@/lib/render/citation-group'

import { StatusPill } from './conclusions'
import { OUTCOME_LABELS } from './vocabulary'

// What changed from the previous version: the deterministic diff recorded with the report
// (by conclusion key) and the changes the investigator stated, with its reasons and refs.
export function VersionChanges({
  report,
  version,
  onCite,
}: {
  report: ReportContent
  version: number
  onCite: (ref: string) => void
}) {
  const diff = report.diff
  const statementOf = (key: string) =>
    report.conclusions.find((c) => c.key === key)?.statement ??
    report.history.find((h) => h.key === key)?.statement ??
    humanize(key.replaceAll('-', ' '))

  if (version === 1) {
    return <p className="text-sm text-muted-foreground">First version of this report.</p>
  }
  const outcomeChanged = diff?.outcome && diff.outcome[0] !== diff.outcome[1]
  const items: { label: string; text: string; detail?: ReactNode }[] = [
    ...(diff?.added ?? []).map((k) => ({ label: 'Added', text: statementOf(k) })),
    ...(diff?.status_changed ?? []).map(([k, before, after]) => ({
      label: 'Status changed',
      text: statementOf(k),
      detail: (
        <span className="inline-flex items-center gap-1">
          <StatusPill status={before} />
          <ArrowRight aria-label="to" className="size-3" />
          <StatusPill status={after} />
        </span>
      ),
    })),
    ...(diff?.retracted ?? []).map((k) => ({ label: 'Retracted', text: statementOf(k) })),
    ...(diff?.no_longer_current ?? []).map((k) => ({
      label: 'No longer current (kept in history)',
      text: statementOf(k),
    })),
    ...(diff?.removed ?? []).map((s) => ({ label: 'Removed', text: s })),
  ]
  const stated = report.changes
  if (!outcomeChanged && items.length === 0 && stated.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No recorded changes from the previous version.
      </p>
    )
  }
  return (
    <div className="space-y-4">
      {outcomeChanged && (
        <p className="flex flex-wrap items-center gap-2 text-sm">
          Outcome:
          <span className="text-muted-foreground">
            {OUTCOME_LABELS[diff!.outcome![0] ?? ''] ?? humanize(diff!.outcome![0] ?? 'none')}
          </span>
          <ArrowRight aria-label="to" className="size-3.5" />
          <span className="font-medium">
            {OUTCOME_LABELS[diff!.outcome![1] ?? ''] ?? humanize(diff!.outcome![1] ?? 'none')}
          </span>
        </p>
      )}
      {items.length > 0 && (
        <ul className="space-y-2" aria-label="Recorded changes">
          {items.map((item, i) => (
            <li key={i} className="space-y-1 text-sm">
              <span className="text-xs font-medium text-muted-foreground uppercase">
                {item.label}
              </span>
              <p>{item.text}</p>
              {item.detail}
            </li>
          ))}
        </ul>
      )}
      {stated.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm font-medium">Changes stated by the investigator</h3>
          <ul className="space-y-2">
            {stated.map((c, i) => (
              <li key={i} className="space-y-1 rounded-md border p-3 text-sm">
                <span className="text-xs font-medium text-muted-foreground uppercase">
                  {humanize(c.change)}
                </span>
                <p>{c.statement}</p>
                {c.reason && <p className="text-xs text-muted-foreground">Why: {c.reason}</p>}
                {c.refs.length > 0 && <CitationGroup refs={c.refs} onSelect={onCite} />}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
