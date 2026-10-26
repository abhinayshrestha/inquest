import type { MetricBlock as MetricBlockData, MetricItem } from '@/lib/api'

import { CitationChip } from '../citation-chip'
import { BlockFrame } from './block-frame'
import { formatValue, STAT_LABELS } from './format'

function sampleText(item: MetricItem) {
  const parts = []
  if (item.sample_count !== null) parts.push(`n = ${item.sample_count.toLocaleString()}`)
  if (item.step_seconds !== null) parts.push(`step ${item.step_seconds} s`)
  return parts.join(' · ')
}

export function MetricBlockView({
  block,
  onCite,
}: {
  block: MetricBlockData
  onCite?: (ref: string) => void
}) {
  const windows = new Set(block.items.map((i) => `${i.window.start}|${i.window.end}`))
  const refs = [...new Set(block.items.map((i) => i.evidence_ref))]
  // One shared window/source goes in the frame; otherwise each card shows its own source.
  const shared = windows.size === 1 && refs.length === 1
  return (
    <BlockFrame
      title={block.title}
      window={shared ? block.items[0]!.window : undefined}
      evidenceRefs={shared ? refs : undefined}
      onCite={onCite}
    >
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {block.items.map((item, i) => (
          <div key={i} className="min-w-0 rounded-md bg-muted/50 px-3 py-2">
            <dt className="truncate text-xs text-muted-foreground" title={item.label}>
              {STAT_LABELS[item.stat] ?? item.stat} · {item.label}
            </dt>
            <dd className="mt-0.5 text-lg font-semibold tabular-nums">
              {formatValue(item.value, item.unit)}
            </dd>
            <dd className="mt-0.5 flex flex-wrap items-center gap-1 text-[0.7rem] text-muted-foreground">
              {sampleText(item)}
              {!shared && <CitationChip citationRef={item.evidence_ref} onSelect={onCite} />}
            </dd>
          </div>
        ))}
      </dl>
    </BlockFrame>
  )
}
