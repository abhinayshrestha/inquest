// Mirrors EVENT_TYPES in src/services/progress.py. The SSE stream names every event
// (`event: <type>`), and EventSource only delivers named events to listeners registered
// for that name, so the list is needed to subscribe. Types added to the backend later are
// simply not shown until they are added here.
export const EVENT_TYPES = [
  'investigation.status_changed',
  'turn.created',
  'turn.queued',
  'turn.started',
  'turn.decision',
  'turn.completed',
  'turn.failed',
  'turn.cancelled',
  'round.started',
  'round.finished',
  'assignment.started',
  'assignment.finished',
  'hypothesis.recorded',
  'hypothesis.updated',
  'tool_call.started',
  'tool_call.finished',
  'finding.recorded',
  'report.revised',
  'budget.exhausted',
] as const

export type EventType = (typeof EVENT_TYPES)[number]

// Events after which the investigation as stored in PostgreSQL has changed in a way the
// conversation shows (status, turns, answers, report versions): refetch it.
export const REFRESH_EVENTS: ReadonlySet<string> = new Set([
  'investigation.status_changed',
  'turn.created',
  'turn.started',
  'turn.completed',
  'turn.failed',
  'turn.cancelled',
  'report.revised',
])
