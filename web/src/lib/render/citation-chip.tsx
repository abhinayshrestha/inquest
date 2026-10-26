import { cn } from 'cn'

import { citationKind } from './citation-kind'

// A ref from an answer, report or activity. Without onSelect it is shown but inert
// (e.g. where no investigation context exists to resolve it).
export function CitationChip({
  citationRef,
  onSelect,
  className,
}: {
  citationRef: string
  onSelect?: (ref: string) => void
  className?: string
}) {
  const { label, icon: Icon } = citationKind(citationRef)
  const classes = cn(
    'inline-flex items-center gap-0.5 rounded bg-muted px-1 py-px align-baseline font-mono text-[0.7rem] leading-tight text-muted-foreground',
    onSelect &&
      'hover:bg-accent hover:text-accent-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
    className,
  )
  const content = (
    <>
      <Icon aria-hidden className="size-2.5 opacity-70" />
      {citationRef}
    </>
  )
  if (!onSelect) return <span className={classes}>{content}</span>
  return (
    <button
      type="button"
      className={classes}
      onClick={() => onSelect(citationRef)}
      aria-label={`${label} ${citationRef}`}
    >
      {content}
    </button>
  )
}
