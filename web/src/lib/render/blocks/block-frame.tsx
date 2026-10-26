import { Clock, Info } from 'lucide-react'
import { useId, type ReactNode } from 'react'

import type { BlockWindow } from '@/lib/api'
import { cn } from 'cn'

import { CitationGroup } from '../citation-group'
import { formatWindow } from './format'

// Shared frame for data-bearing blocks: title, observation window, source evidence and an
// optional note (sample sizes, gaps). Every number shown inside comes from those sources.
export function BlockFrame({
  title,
  window,
  evidenceRefs,
  note,
  onCite,
  children,
  className,
}: {
  title: string
  window?: BlockWindow
  evidenceRefs?: string[]
  note?: string | null
  onCite?: (ref: string) => void
  children: ReactNode
  className?: string
}) {
  const titleId = useId()
  return (
    <figure aria-labelledby={titleId} className={cn('rounded-lg border bg-card p-4', className)}>
      <figcaption className="mb-3 space-y-1">
        <h3 id={titleId} className="text-sm font-semibold">
          {title}
        </h3>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          {window && (
            <span className="inline-flex items-center gap-1">
              <Clock aria-hidden className="size-3" />
              {formatWindow(window)}
            </span>
          )}
          {evidenceRefs && evidenceRefs.length > 0 && (
            <span className="inline-flex items-center gap-1">
              Source <CitationGroup refs={evidenceRefs} onSelect={onCite} />
            </span>
          )}
        </div>
      </figcaption>
      {children}
      {note && (
        <p className="mt-3 flex items-start gap-1.5 text-xs text-muted-foreground">
          <Info aria-hidden className="mt-0.5 size-3 shrink-0" />
          {note}
        </p>
      )}
    </figure>
  )
}
