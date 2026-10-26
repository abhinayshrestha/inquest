import { cn } from 'cn'

type Tone = 'neutral' | 'running' | 'success' | 'warning' | 'danger'

const TONES: Record<Tone, string> = {
  neutral: 'bg-muted text-muted-foreground border-transparent',
  running: 'bg-info/15 text-info border-info/30',
  success: 'bg-success/15 text-success border-success/30',
  warning: 'bg-warning/15 text-warning border-warning/30',
  danger: 'bg-destructive/15 text-destructive border-destructive/30',
}

// Investigation, turn and assignment statuses share these values (src/models/).
const STATUS_TONE: Record<string, Tone> = {
  pending: 'neutral',
  investigating: 'running',
  correlating: 'running',
  running: 'running',
  idle: 'success',
  completed: 'success',
  failed: 'danger',
  timed_out: 'warning',
  cancelled: 'warning',
}

export function StatusBadge({ status, className }: { status: string; className?: string }) {
  const tone = STATUS_TONE[status] ?? 'neutral'
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-medium',
        TONES[tone],
        className,
      )}
    >
      {tone === 'running' && (
        <span aria-hidden className="size-1.5 animate-pulse rounded-full bg-current" />
      )}
      {status.replaceAll('_', ' ')}
    </span>
  )
}
