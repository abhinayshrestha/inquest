import { z } from '@/lib/zod'

// Response blocks, version 1: mirrors src/schemas/presentation.py. Objects are strict
// (unknown keys rejected) and numbers must be finite, matching the backend, so a tampered
// or malformed block fails here and renders as a fallback instead.
//
// The envelope is parsed first with blocks left unknown; each block is then parsed on its
// own, so one bad block degrades to a notice while the rest of the answer still renders.

const evidenceRef = z.string().regex(/^ev_[0-9a-f]{8}$/)
const key = z.string().regex(/^[a-z][a-z0-9_]{0,23}$/)
const label = z.string().min(1).max(120)
const unit = z.string().min(1).max(24)
const finite = z.number().refine(Number.isFinite, 'not a finite number')
const timestamp = z.iso.datetime({ offset: true })

export const blockWindow = z
  .strictObject({ start: timestamp, end: timestamp })
  .refine((w) => Date.parse(w.end) >= Date.parse(w.start), 'window ends before it starts')

export const markdownBlock = z.strictObject({
  type: z.literal('markdown'),
  markdown: z.string().min(1).max(8000),
})

export const codeBlock = z.strictObject({
  type: z.literal('code'),
  language: z.string().regex(/^[a-z0-9+#-]{0,20}$/),
  code: z.string().min(1).max(4000),
})

export const metricItem = z.strictObject({
  label,
  stat: z.enum([
    'mean',
    'median',
    'min',
    'max',
    'last',
    'total',
    'count',
    'rate',
    'p50',
    'p95',
    'p99',
  ]),
  value: finite,
  unit: unit.nullable(),
  sample_count: z.int().min(0).nullable(),
  step_seconds: z.int().min(1).nullable(),
  window: blockWindow,
  evidence_ref: evidenceRef,
})

export const metricBlock = z.strictObject({
  type: z.literal('metric'),
  title: label,
  items: z.array(metricItem).min(1).max(8),
})

const column = z.strictObject({
  key,
  label,
  kind: z.enum(['text', 'number']),
  unit: unit.nullable(),
})

export const tableBlock = z
  .strictObject({
    type: z.literal('table'),
    title: label,
    columns: z.array(column).min(1).max(8),
    rows: z
      .array(z.record(key, z.union([z.string().max(200), finite, z.null()])))
      .min(1)
      .max(50),
    evidence_refs: z.array(evidenceRef).min(1).max(8),
    window: blockWindow,
    note: z.string().max(400).nullable(),
  })
  .refine(
    (t) => {
      const kinds = new Map(t.columns.map((c) => [c.key, c.kind]))
      return t.rows.every((row) =>
        Object.entries(row).every(
          ([k, v]) =>
            kinds.has(k) && (v === null || (kinds.get(k) === 'number') === (typeof v === 'number')),
        ),
      )
    },
    { message: 'cells do not match columns' },
  )

const chartPoint = z.strictObject({
  x: z.string().min(1).max(120),
  values: z.record(key, finite.nullable()),
  sample_count: z.int().min(0).nullable(),
})

export const chartBlock = z
  .strictObject({
    type: z.literal('chart'),
    kind: z.enum(['bar', 'line']),
    title: label,
    x_label: label,
    series: z
      .array(z.strictObject({ key, label, unit: unit.nullable() }))
      .min(1)
      .max(4),
    points: z.array(chartPoint).min(2).max(120),
    evidence_refs: z.array(evidenceRef).min(1).max(8),
    window: blockWindow,
    note: z.string().max(400).nullable(),
  })
  .refine(
    (c) => {
      const keys = new Set(c.series.map((s) => s.key))
      if (!c.points.every((p) => Object.keys(p.values).every((k) => keys.has(k)))) return false
      if (c.kind !== 'line') return true
      const times = c.points.map((p) => Date.parse(p.x))
      return times.every((t, i) => Number.isFinite(t) && (i === 0 || t >= times[i - 1]!))
    },
    { message: 'points do not match series or are not a time series' },
  )

export const evidenceBlock = z.strictObject({
  type: z.literal('evidence'),
  items: z
    .array(
      z.strictObject({
        ref: evidenceRef,
        title: z.string().min(1).max(255),
        evidence_type: z.string().min(1).max(40),
        summary: z.string().max(400),
        window: blockWindow.nullable(),
      }),
    )
    .min(1)
    .max(12),
})

export const responseBlock = z.discriminatedUnion('type', [
  markdownBlock,
  codeBlock,
  metricBlock,
  tableBlock,
  chartBlock,
  evidenceBlock,
])

export const presentationEnvelope = z.object({
  version: z.literal(1),
  turn_id: z.uuid(),
  generated_at: timestamp,
  blocks: z.array(z.unknown()).min(1).max(16),
})

export type BlockWindow = z.infer<typeof blockWindow>
export type MarkdownBlock = z.infer<typeof markdownBlock>
export type CodeBlock = z.infer<typeof codeBlock>
export type MetricBlock = z.infer<typeof metricBlock>
export type MetricItem = z.infer<typeof metricItem>
export type TableBlock = z.infer<typeof tableBlock>
export type ChartBlock = z.infer<typeof chartBlock>
export type EvidenceBlock = z.infer<typeof evidenceBlock>
export type ResponseBlock = z.infer<typeof responseBlock>
export type PresentationEnvelope = z.infer<typeof presentationEnvelope>
