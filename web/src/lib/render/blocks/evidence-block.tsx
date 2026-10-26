import type { EvidenceBlock as EvidenceBlockData } from '@/lib/api'
import { humanize } from '@/lib/format'

import { CitationChip } from '../citation-chip'
import { formatWindow } from './format'

// The evidence the answer cites, as recorded: what was observed, where and when. Opening
// a chip shows the full citation.
export function EvidenceBlockView({
  block,
  onCite,
}: {
  block: EvidenceBlockData
  onCite?: (ref: string) => void
}) {
  return (
    <section aria-label="Cited evidence" className="rounded-lg border p-4">
      <h3 className="mb-2 text-sm font-semibold">Cited evidence</h3>
      <ul className="space-y-2.5">
        {block.items.map((item) => (
          <li key={item.ref} className="min-w-0 text-sm">
            <div className="flex flex-wrap items-center gap-1.5">
              <CitationChip citationRef={item.ref} onSelect={onCite} />
              <span className="text-xs text-muted-foreground">
                {humanize(item.evidence_type)}
                {item.window && ` · ${formatWindow(item.window)}`}
              </span>
            </div>
            <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground" title={item.summary}>
              {item.summary || item.title}
            </p>
          </li>
        ))}
      </ul>
    </section>
  )
}
