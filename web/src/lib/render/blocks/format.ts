import type { BlockWindow } from '@/lib/api'

// Units as recorded by the backend (SigNoz units, "ms", "spans", "%") shown compactly.
const UNIT_DISPLAY: Record<string, string> = { percent: '%', count: '', records: 'records' }

export function unitLabel(unit: string | null | undefined): string {
  if (!unit) return ''
  return UNIT_DISPLAY[unit] ?? unit
}

const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 })

export function formatValue(value: number | null | undefined, unit?: string | null): string {
  if (value === null || value === undefined) return '—'
  const u = unitLabel(unit)
  const n = number.format(value)
  if (!u) return n
  return u === '%' ? `${n}%` : `${n} ${u}`
}

const time = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
  timeZone: 'UTC',
  hour12: false,
})
const day = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })

export function formatTime(value: string | number): string {
  return time.format(new Date(value))
}

// Observation windows are always shown in UTC: the times in answers and evidence are UTC.
export function formatWindow(window: BlockWindow): string {
  const start = new Date(window.start)
  const end = new Date(window.end)
  const sameDay = day.format(start) === day.format(end)
  return sameDay
    ? `${day.format(start)}, ${time.format(start)}–${time.format(end)} UTC`
    : `${day.format(start)} ${time.format(start)} – ${day.format(end)} ${time.format(end)} UTC`
}

export const STAT_LABELS: Record<string, string> = {
  mean: 'Mean',
  median: 'Median',
  min: 'Min',
  max: 'Peak',
  last: 'Latest',
  total: 'Total',
  count: 'Count',
  rate: 'Rate',
  p50: 'p50',
  p95: 'p95',
  p99: 'p99',
}
