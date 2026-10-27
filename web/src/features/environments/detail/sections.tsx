import { AlertTriangle, CheckCircle2, Lock } from 'lucide-react'
import type { ReactNode } from 'react'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from 'cn'

// Small building blocks shared by the environment sections.

export function Section({
  title,
  description,
  badge,
  children,
  className,
}: {
  title: string
  description?: ReactNode
  badge?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2 text-base">
          {title}
          {badge}
        </CardTitle>
        {description && <CardDescription>{description}</CardDescription>}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

export function PlannedBadge({ children = 'Planned' }: { children?: ReactNode }) {
  return (
    <Badge variant="outline" className="gap-1 font-normal">
      <Lock aria-hidden className="size-3" />
      {children}
    </Badge>
  )
}

export function ConfiguredBadge({ configured }: { configured: boolean }) {
  return configured ? (
    <Badge variant="secondary" className="gap-1 font-normal">
      <CheckCircle2 aria-hidden className="size-3 text-success" />
      Configured
    </Badge>
  ) : (
    <Badge variant="outline" className="gap-1 font-normal text-warning">
      <AlertTriangle aria-hidden className="size-3" />
      Not configured
    </Badge>
  )
}

export function Facts({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
      {items.map(([label, value]) => (
        <div key={label} className="min-w-0">
          <dt className="text-xs text-muted-foreground">{label}</dt>
          <dd className="mt-0.5 break-words">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

export function Notice({
  tone = 'info',
  children,
}: {
  tone?: 'info' | 'warning'
  children: ReactNode
}) {
  return (
    <p
      className={cn(
        'rounded-md border px-3 py-2 text-xs',
        tone === 'warning'
          ? 'border-warning/30 bg-warning/10'
          : 'border-info/30 bg-info/10 text-foreground',
      )}
    >
      {children}
    </p>
  )
}
