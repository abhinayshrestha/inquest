import { z } from 'zod'

// The JSON documents inside turns (answer_json) and reports (report_json). Written by
// src/investigators/main_graph.py persist (+ services/turns.complete_turn) and shaped by
// AnswerDraft/ReportDraft and main_validation.merge_report/report_diff. Parsed where they
// are rendered, not in the API client, so one malformed document degrades one view, not
// the page. Unknown keys (validation_audit, unresolved_problems, ...) are ignored on
// purpose: they are internal diagnostics. Reports written by earlier phases differ (no
// claim, legacy diff keys, changes: null), so every optional part defaults safely.

export const answerContent = z.object({
  answer_md: z.string(),
  fallback: z.boolean().catch(false),
  report_rejected: z.array(z.string()).catch([]),
  rounds: z.int().nullish(),
  limited: z.boolean().catch(false),
  window: z.string().nullish(),
  report_version: z.int().nullish(),
  current_report_version: z.int().nullish(),
})

const REF = /^(ev|fd|hy)_[0-9a-f]{8}$/

// Stored citations are {id, ref, kind} objects; older fixtures and drafts used plain
// strings. Both become ref strings; anything else is dropped rather than rendered.
const refs = z
  .array(z.unknown())
  .catch([])
  .transform((items) =>
    items
      .map((r) =>
        typeof r === 'string' ? r : r && typeof r === 'object' && 'ref' in r ? String(r.ref) : '',
      )
      .filter((r) => REF.test(r)),
  )

const text = z.string().catch('')
const optionalText = z.string().nullish().catch(null)
const strings = z
  .array(z.unknown())
  .nullish()
  .catch([])
  .transform((v) => (v ?? []).filter((s): s is string => typeof s === 'string'))

export const CLAIMS = ['observation', 'temporal_overlap', 'correlation', 'causal'] as const

export const reportConclusion = z.object({
  key: optionalText,
  claim: z.enum(CLAIMS).nullish().catch(null),
  statement: z.string(),
  status: z.string(),
  confidence: optionalText,
  supporting: refs,
  contradicting: refs,
  // When the evidence behind it was observed (latest evidence window end), not when the
  // report was written.
  as_of: optionalText,
})

export const historicalConclusion = z.object({
  key: optionalText,
  statement: z.string(),
  status: z.string(),
  supporting: refs,
  as_of: optionalText,
  from_version: z.int().nullish().catch(null),
})

const pair = z.tuple([z.string().nullable(), z.string().nullable()])

export const reportDiff = z
  .object({
    material: z.boolean().catch(false),
    outcome: pair.nullish().catch(null),
    added: strings,
    // [key, before, after]
    status_changed: z
      .array(z.unknown())
      .catch([])
      .transform((v) =>
        v.filter(
          (x): x is [string, string, string] =>
            Array.isArray(x) && x.length === 3 && x.every((s) => typeof s === 'string'),
        ),
      ),
    no_longer_current: strings,
    retracted: strings,
    // Early reports diffed by statement text instead of key.
    removed: strings,
  })
  .nullish()
  .catch(null)

export const reportChange = z.object({
  change: z.string(),
  key: optionalText,
  statement: z.string(),
  reason: text,
  refs,
})

export const reportContent = z.object({
  outcome: z.string(),
  summary: z.string(),
  conclusions: z.array(reportConclusion).catch([]),
  history: z
    .array(historicalConclusion)
    .nullish()
    .catch([])
    .transform((v) => v ?? []),
  timeline: z
    .array(z.object({ at: z.string(), event: z.string(), refs }))
    .nullish()
    .catch([])
    .transform((v) => v ?? []),
  gaps: strings,
  next_checks: strings,
  changes: z
    .array(reportChange)
    .nullish()
    .catch([])
    .transform((v) => v ?? []),
  diff: reportDiff,
})

export type AnswerContent = z.infer<typeof answerContent>
export type ReportConclusion = z.infer<typeof reportConclusion>
export type HistoricalConclusion = z.infer<typeof historicalConclusion>
export type ReportDiff = NonNullable<z.infer<typeof reportDiff>>
export type ReportContent = z.infer<typeof reportContent>
export type Claim = (typeof CLAIMS)[number]
