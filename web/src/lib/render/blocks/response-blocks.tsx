import { AlertTriangle } from 'lucide-react'
import { lazy, Suspense } from 'react'

import { ErrorBoundary } from '@/components/error-boundary'
import { Skeleton } from '@/components/ui/skeleton'
import { responseBlock, type ResponseBlock } from '@/lib/api'

import { CodeView } from '../code-view'
import { Markdown } from '../markdown'
import { EvidenceBlockView } from './evidence-block'
import { MetricBlockView } from './metric-block'
import { TableBlockView } from './table-block'

// The explicit registry of response components (D023). A block renders only if it parses
// against its schema and its type is listed here; anything else (unknown type, tampered
// fields, a rendering error) becomes a neutral notice while the other blocks still render.
// Recharts is loaded only for answers that actually contain a chart.

const ChartBlockView = lazy(() => import('./chart-block'))

type OnCite = ((ref: string) => void) | undefined

function ChartSkeleton() {
  return (
    <div role="status" aria-label="Loading chart" className="space-y-3 rounded-lg border p-4">
      <Skeleton className="h-4 w-48" />
      <Skeleton className="h-48 w-full" />
    </div>
  )
}

export function BlockFallback() {
  return (
    <p className="flex items-center gap-2 rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">
      <AlertTriangle aria-hidden className="size-3.5" />
      This part of the answer could not be displayed.
    </p>
  )
}

function BlockView({ block, onCite }: { block: ResponseBlock; onCite: OnCite }) {
  switch (block.type) {
    case 'markdown':
      return <Markdown onCite={onCite}>{block.markdown}</Markdown>
    case 'code':
      return <CodeView code={block.code} language={block.language} />
    case 'metric':
      return <MetricBlockView block={block} onCite={onCite} />
    case 'table':
      return <TableBlockView block={block} onCite={onCite} />
    case 'chart':
      return (
        <Suspense fallback={<ChartSkeleton />}>
          <ChartBlockView block={block} onCite={onCite} />
        </Suspense>
      )
    case 'evidence':
      return <EvidenceBlockView block={block} onCite={onCite} />
  }
}

export function ResponseBlocks({
  blocks,
  onCite,
}: {
  blocks: readonly unknown[]
  onCite?: OnCite
}) {
  return (
    <div className="space-y-4">
      {blocks.map((raw, i) => {
        const parsed = responseBlock.safeParse(raw)
        if (!parsed.success) {
          if (import.meta.env.DEV) console.warn('Invalid response block', i, parsed.error.issues)
          return <BlockFallback key={i} />
        }
        return (
          <ErrorBoundary key={i} fallback={() => <BlockFallback />}>
            <BlockView block={parsed.data} onCite={onCite} />
          </ErrorBoundary>
        )
      })}
    </div>
  )
}
