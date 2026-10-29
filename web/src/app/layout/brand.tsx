import { Link } from 'react-router'

import { cn } from 'cn'
import { LogoMark, Wordmark } from '@/components/logo'

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
      <LogoMark />
      {!compact && <Wordmark />}
    </Link>
  )
}
