import { z } from 'zod'

// The JSON documents inside turns (answer_json) and reports (report_json). Written by
// src/investigators/main_graph.py persist (+ services/turns.complete_turn) and shaped by
// AnswerDraft/ReportDraft in src/investigators/schemas.py. Parsed where they are rendered,
// not in the API client, so one malformed document degrades one turn, not the page.
// Unknown keys (validation_audit, unresolved_problems, ...) are ignored on purpose: they
// are internal diagnostics, not user-facing content.

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

const refs = z.array(z.string()).catch([])

export const reportConclusion = z.object({
  key: z.string().nullish(),
  claim: z.string().nullish(),
  statement: z.string(),
  status: z.string(),
  confidence: z.string().nullish(),
  supporting: refs,
  contradicting: refs,
  as_of: z.string().nullish(),
})

export const reportContent = z.object({
  outcome: z.string(),
  summary: z.string(),
  conclusions: z.array(reportConclusion).catch([]),
  timeline: z.array(z.object({ at: z.string(), event: z.string(), refs })).catch([]),
  gaps: z.array(z.string()).catch([]),
  next_checks: z.array(z.string()).catch([]),
  changes: z
    .array(
      z.object({
        change: z.string(),
        statement: z.string(),
        reason: z.string().catch(''),
        refs,
      }),
    )
    .catch([]),
})

export type AnswerContent = z.infer<typeof answerContent>
export type ReportConclusion = z.infer<typeof reportConclusion>
export type ReportContent = z.infer<typeof reportContent>
