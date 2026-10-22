import type { ReactNode } from 'react'

import { Skeleton } from '@/components/ui/skeleton'

// Layout-shaped placeholders, so pages do not jump when content arrives. Each wrapper is
// one status region with a label; the individual bars are hidden from assistive tech.

function Busy({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div role="status" aria-busy="true" aria-label={label}>
      <div aria-hidden>{children}</div>
    </div>
  )
}

export function PageHeaderSkeleton() {
  return (
    <div className="space-y-2 pb-6">
      <Skeleton className="h-7 w-48" />
      <Skeleton className="h-4 w-80 max-w-full" />
    </div>
  )
}

export function ListSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="divide-y rounded-lg border">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-4 p-4">
          <Skeleton className="size-8 rounded-full" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-4 w-2/5" />
            <Skeleton className="h-3 w-3/5" />
          </div>
          <Skeleton className="hidden h-6 w-20 sm:block" />
        </div>
      ))}
    </div>
  )
}

export function CardGridSkeleton({ cards = 3 }: { cards?: number }) {
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      {Array.from({ length: cards }, (_, i) => (
        <div key={i} className="space-y-3 rounded-lg border p-5">
          <Skeleton className="h-4 w-1/3" />
          <Skeleton className="h-8 w-1/2" />
          <Skeleton className="h-3 w-4/5" />
        </div>
      ))}
    </div>
  )
}

export function DetailSkeleton() {
  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-3">
        {Array.from({ length: 3 }, (_, i) => (
          <Skeleton key={i} className="h-20" />
        ))}
      </div>
      <div className="space-y-3 rounded-lg border p-5">
        <Skeleton className="h-4 w-1/4" />
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-11/12" />
        <Skeleton className="h-3 w-3/4" />
      </div>
    </div>
  )
}

export type PageSkeletonVariant = 'cards' | 'list' | 'detail'

export function PageSkeleton({ variant = 'list' }: { variant?: PageSkeletonVariant }) {
  return (
    <Busy label="Loading page">
      <PageHeaderSkeleton />
      {variant === 'cards' && <CardGridSkeleton />}
      {variant === 'list' && <ListSkeleton />}
      {variant === 'detail' && <DetailSkeleton />}
    </Busy>
  )
}

export function SectionSkeleton({ label = 'Loading' }: { label?: string }) {
  return (
    <Busy label={label}>
      <DetailSkeleton />
    </Busy>
  )
}
