import { ScanSearch } from 'lucide-react'
import { Link } from 'react-router'

import { cn } from 'cn'

export function Brand({
  compact = false,
  onNavigate,
}: {
  compact?: boolean
  onNavigate?: () => void
}) {
  return (
    <Link
      to="/"
      onClick={onNavigate}
      aria-label="Inquest home"
      className={cn(
        'flex items-center gap-2 rounded-md font-semibold outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring',
        compact && 'justify-center',
      )}
    >
      <span className="flex size-8 items-center justify-center rounded-md bg-sidebar-primary text-sidebar-primary-foreground">
        <ScanSearch aria-hidden className="size-4" />
      </span>
      {!compact && <span className="text-base tracking-tight">Inquest</span>}
    </Link>
  )
}
