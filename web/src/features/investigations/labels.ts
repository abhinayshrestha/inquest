import { humanize } from '@/lib/format'

// Human-readable names for deterministic backend values. Unknown values fall back to a
// humanized form of the value itself, so new backend codes still render sensibly.

// MCP exposes registry tools with underscores (src/mcp_server); ToolCall rows keep dots.
const TOOLS: Record<string, string> = {
  signoz_query_metrics: 'Query metrics',
  signoz_search_logs: 'Search logs',
  signoz_log_volume: 'Log volume',
  signoz_search_spans: 'Search spans',
  signoz_trace_stats: 'Trace statistics',
  signoz_get_trace: 'Get trace',
}

export function toolLabel(tool: string): string {
  return TOOLS[tool.replaceAll('.', '_')] ?? humanize(tool.replaceAll('.', ' '))
}

export function toolSource(tool: string): string | null {
  return tool.startsWith('signoz') ? 'SigNoz' : null
}

export const DECISION_LABELS: Record<string, string> = {
  answer: 'Answering from existing evidence',
  investigate: 'Gathering new evidence',
  clarify: 'Asking for clarification',
}

export const TURN_DECISION_LABELS: Record<string, string> = {
  reused_evidence: 'Answered from existing evidence',
  investigated: 'Investigated',
  clarification: 'Needs clarification',
}

const ERROR_CODES: Record<string, string> = {
  turn_timeout: 'The turn ran out of time.',
  main_error: 'The investigator hit an unexpected error.',
  answer_invalid: 'No answer could be validated against the evidence.',
  legacy_no_checkpoint: 'The turn was interrupted and could not be resumed.',
  cancelled: 'The turn was cancelled.',
}

export function turnErrorMessage(code: string | null): string {
  if (!code) return 'The turn did not finish.'
  return ERROR_CODES[code] ?? `The turn did not finish (${code}).`
}

export function toolStatusLabel(status: string, outcome: string | null): string {
  if (status === 'succeeded') {
    if (outcome === 'empty') return 'No matching data'
    if (outcome === 'not_found') return 'Not found'
    return 'Observed'
  }
  if (status === 'running') return 'Running'
  if (status === 'transport_error') return 'Connection failed'
  return humanize(status)
}

export const TELEMETRY_LABELS: Record<string, string> = {
  yes: 'Telemetry available',
  no: 'No telemetry for this scope',
  unknown: 'Telemetry coverage unknown',
}
