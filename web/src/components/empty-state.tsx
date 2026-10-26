import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'

import { Badge } from '@/components/ui/badge'
import { cn } from 'cn'

type EmptyStateProps = {
  icon: LucideIcon
  title: string
  description: ReactNode
  // Backend routes this view needs that do not exist yet. Shown verbatim so it is
  // obvious the view is empty because of a backend gap, not because there is no data.
  requires?: readonly string[]
  badge?: string
  action?: ReactNode
  className?: string
}

export function EmptyState({
  icon: Icon,
  title,
  description,
  requires,
  badge,
  action,
  className,
}: EmptyStateProps) {
  return (
    <section
      aria-label={title}
      className={cn(
        'flex flex-col items-center gap-3 rounded-lg border border-dashed px-6 py-10 text-center',
        className,
      )}
    >
      <div className="flex size-12 items-center justify-center rounded-full bg-muted text-muted-foreground">
        <Icon aria-hidden className="size-6" />
      </div>
      {badge && <Badge variant="outline">{badge}</Badge>}
      <h2 className="text-base font-semibold">{title}</h2>
      <div className="max-w-prose text-sm text-muted-foreground">{description}</div>
      {requires && requires.length > 0 && (
        <div className="text-xs text-muted-foreground">
          <p className="mb-1 font-medium">Requires backend endpoint (not implemented):</p>
          <ul className="space-y-0.5">
            {requires.map((route) => (
              <li key={route}>
                <code className="rounded bg-muted px-1.5 py-0.5 font-mono">{route}</code>
              </li>
            ))}
          </ul>
        </div>
      )}
      {action && <div className="mt-2">{action}</div>}
    </section>
  )
}
