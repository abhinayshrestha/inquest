import { Loader2 } from 'lucide-react'

import { cn } from 'cn'

export function Spinner({ className, label = 'Loading' }: { className?: string; label?: string }) {
  return (
    <span role="status" className={cn('inline-flex items-center gap-2', className)}>
      <Loader2 aria-hidden className="size-4 animate-spin" />
      <span className="sr-only">{label}</span>
    </span>
  )
}

export function LoadingState({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
      <Spinner />
      <span aria-hidden>{label}…</span>
    </div>
  )
}
