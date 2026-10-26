import { useState } from 'react'

import { CitationChip } from './citation-chip'

const VISIBLE = 3

// A run of refs from one place in the text ("[ev_a, ev_b, …]"). Long runs show the first
// two and a "+N" toggle, so citations support the prose instead of drowning it.
export function CitationGroup({
  refs,
  onSelect,
}: {
  refs: string[]
  onSelect?: (ref: string) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const collapsed = !expanded && refs.length > VISIBLE
  const shown = collapsed ? refs.slice(0, VISIBLE - 1) : refs
  return (
    <span className="mx-0.5 inline-flex flex-wrap items-baseline gap-1 align-baseline">
      {shown.map((ref) => (
        <CitationChip key={ref} citationRef={ref} onSelect={onSelect} />
      ))}
      {collapsed && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="rounded px-1 text-[0.7rem] font-medium text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          aria-label={`Show ${refs.length - shown.length} more citations`}
        >
          +{refs.length - shown.length}
        </button>
      )}
    </span>
  )
}
