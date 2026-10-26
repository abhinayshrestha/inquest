import type { ProgressEvent } from '@/lib/api'

// Folds deterministic progress events into what the activity panel shows, per turn.
// Payloads are read field by field with type checks: anything unexpected is skipped,
// never rendered raw. Events replayed after a backend crash carry new ids, so operations
// are keyed by their own identity (tool call id, hypothesis/finding ref) and updated in
// place instead of appearing twice.

export type ToolStatus =
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'rejected'
  | 'timed_out'
  | 'cancelled'
  | 'transport_error'
  | 'unknown'

export type ToolCallItem = {
  kind: 'tool'
  key: string
  tool: string
  scope: ScopeEntry[]
  status: ToolStatus
  outcome: string | null
  telemetryAvailable: string | null
  evidence: string[]
  errorCode: string | null
  replayed: boolean
  startedAt: string
  finishedAt: string | null
}

export type HypothesisItem = {
  kind: 'hypothesis'
  key: string
  ref: string
  statement: string | null
  status: string
}

export type FindingItem = {
  kind: 'finding'
  key: string
  ref: string
  title: string | null
  status: string | null
}

export type AssignmentItem = ToolCallItem | HypothesisItem | FindingItem

export type AssignmentStep = {
  kind: 'assignment'
  key: string
  round: number | null
  environmentId: string | null
  objective: string | null
  status: string
  items: AssignmentItem[]
  startedAt: string
  finishedAt: string | null
  budget: string | null
}

export type DecisionStep = {
  kind: 'decision'
  key: string
  decision: string
  round: number | null
  objectives: string[]
  gaps: number | null
  limited: boolean
  at: string
}

export type NoticeStep = {
  kind: 'notice'
  key: string
  tone: 'neutral' | 'success' | 'warning' | 'danger'
  label: string
  at: string
}

export type ActivityStep = AssignmentStep | DecisionStep | NoticeStep

export type TurnActivity = {
  turnId: string
  steps: ActivityStep[]
  queued: boolean
  started: boolean
  finished: 'completed' | 'failed' | 'cancelled' | null
  toolCalls: number
  evidence: number
  findings: number
  firstAt: string
  lastAt: string
}

export type ScopeEntry = { label: string; value: string }

type Payload = Record<string, unknown>

const str = (p: Payload, key: string) => (typeof p[key] === 'string' ? (p[key] as string) : null)
const num = (p: Payload, key: string) => (typeof p[key] === 'number' ? (p[key] as number) : null)
const strings = (p: Payload, key: string) =>
  Array.isArray(p[key]) ? (p[key] as unknown[]).filter((v) => typeof v === 'string') : []

const TOOL_STATUSES = new Set<ToolStatus>([
  'succeeded',
  'failed',
  'rejected',
  'timed_out',
  'cancelled',
  'transport_error',
])

// Allowlisted argument fields shown as query scope. The backend sends a compact, clipped
// JSON preview of the model's tool arguments (possibly truncated mid-string), so fields
// are matched individually rather than parsed. Free-text filters (body_contains, where
// conditions) are deliberately not shown.
const SCOPE_FIELDS: readonly [string, string][] = [
  ['identity', 'Identity'],
  ['service', 'Service'],
  ['host', 'Host'],
  ['metric', 'Metric'],
  ['metric_name', 'Metric'],
  ['operation', 'Operation'],
  ['group_by', 'Group by'],
  ['log_file', 'Log file'],
  ['trace_id', 'Trace'],
]
const MAX_SCOPE_VALUE = 60

function clip(value: string) {
  return value.length > MAX_SCOPE_VALUE ? `${value.slice(0, MAX_SCOPE_VALUE)}…` : value
}

function timeOf(iso: string) {
  const match = /T(\d{2}:\d{2})/.exec(iso)
  return match ? match[1] : null
}

export function toolScope(preview: string | null): ScopeEntry[] {
  if (!preview) return []
  const entries: ScopeEntry[] = []
  const start = /"start":"([^"]+)"/.exec(preview)?.[1]
  const end = /"end":"([^"]+)"/.exec(preview)?.[1]
  if (start && end && timeOf(start) && timeOf(end)) {
    entries.push({ label: 'Window', value: `${timeOf(start)}–${timeOf(end)} UTC` })
  }
  for (const [field, label] of SCOPE_FIELDS) {
    const value = new RegExp(`"${field}":"([^"]{1,200})"`).exec(preview)?.[1]
    if (value) entries.push({ label, value: clip(value) })
  }
  const severity = /"severity":\[([^\]]{1,120})\]/.exec(preview)?.[1]
  if (severity) {
    const levels = [...severity.matchAll(/"([^"]{1,20})"/g)].map((m) => m[1])
    if (levels.length) entries.push({ label: 'Severity', value: clip(levels.join(', ')) })
  }
  if (/"errors_only":true/.test(preview)) entries.push({ label: 'Filter', value: 'errors only' })
  return entries
}

function emptyTurn(turnId: string, at: string): TurnActivity {
  return {
    turnId,
    steps: [],
    queued: false,
    started: false,
    finished: null,
    toolCalls: 0,
    evidence: 0,
    findings: 0,
    firstAt: at,
    lastAt: at,
  }
}

const DECISION_LABEL_KEYS = new Set(['answer', 'investigate', 'clarify'])

function assignmentStep(turn: TurnActivity, key: string, at: string): AssignmentStep {
  let step = turn.steps.find((s): s is AssignmentStep => s.kind === 'assignment' && s.key === key)
  if (!step) {
    step = {
      kind: 'assignment',
      key,
      round: null,
      environmentId: null,
      objective: null,
      status: 'running',
      items: [],
      startedAt: at,
      finishedAt: null,
      budget: null,
    }
    turn.steps.push(step)
  }
  return step
}

function notice(
  turn: TurnActivity,
  key: string,
  label: string,
  tone: NoticeStep['tone'],
  at: string,
) {
  if (!turn.steps.some((s) => s.key === key))
    turn.steps.push({ kind: 'notice', key, label, tone, at })
}

function apply(turn: TurnActivity, event: ProgressEvent) {
  const p = event.payload
  const at = event.created_at
  turn.lastAt = at
  switch (event.type) {
    case 'turn.queued':
      turn.queued = true
      notice(turn, 'queued', 'Queued', 'neutral', at)
      return
    case 'turn.started':
      turn.started = true
      notice(turn, 'started', 'Investigation turn started', 'neutral', at)
      return
    case 'turn.decision': {
      const decision = str(p, 'decision')
      if (!decision || !DECISION_LABEL_KEYS.has(decision)) return
      const round = num(p, 'round')
      turn.steps.push({
        kind: 'decision',
        key: `decision:${turn.steps.length}`,
        decision,
        round,
        objectives: strings(p, 'objectives'),
        gaps: num(p, 'gaps'),
        limited: p.limited === true,
        at,
      })
      return
    }
    case 'assignment.started': {
      if (!event.assignment_id) return
      const step = assignmentStep(turn, event.assignment_id, at)
      step.round = num(p, 'round') ?? step.round
      step.environmentId = str(p, 'environment_id') ?? step.environmentId
      step.objective = str(p, 'objective') ?? step.objective
      step.status = 'running'
      return
    }
    case 'assignment.finished': {
      if (!event.assignment_id) return
      const step = assignmentStep(turn, event.assignment_id, at)
      step.status = str(p, 'status') ?? 'completed'
      step.finishedAt = at
      return
    }
    case 'tool_call.started':
    case 'tool_call.finished': {
      const callId = str(p, 'call_id')
      if (!event.assignment_id || !callId) return
      const step = assignmentStep(turn, event.assignment_id, at)
      let item = step.items.find((i): i is ToolCallItem => i.kind === 'tool' && i.key === callId)
      if (!item) {
        item = {
          kind: 'tool',
          key: callId,
          tool: str(p, 'tool') ?? 'tool',
          scope: [],
          status: 'running',
          outcome: null,
          telemetryAvailable: null,
          evidence: [],
          errorCode: null,
          replayed: false,
          startedAt: at,
          finishedAt: null,
        }
        step.items.push(item)
      }
      if (event.type === 'tool_call.started') {
        item.scope = toolScope(str(p, 'arguments'))
        return
      }
      const status = str(p, 'status') as ToolStatus | null
      item.status = status && TOOL_STATUSES.has(status) ? status : 'unknown'
      item.outcome = str(p, 'outcome')
      item.telemetryAvailable = str(p, 'telemetry_available')
      item.evidence = strings(p, 'evidence').filter((r) => /^ev_[0-9a-f]{8}$/.test(r))
      item.errorCode = str(p, 'error_code')
      item.replayed = p.replayed === true
      item.finishedAt = at
      return
    }
    case 'hypothesis.recorded':
    case 'hypothesis.updated': {
      const ref = str(p, 'ref')
      if (!event.assignment_id || !ref) return
      const step = assignmentStep(turn, event.assignment_id, at)
      let item = step.items.find(
        (i): i is HypothesisItem => i.kind === 'hypothesis' && i.ref === ref,
      )
      if (!item) {
        item = { kind: 'hypothesis', key: ref, ref, statement: null, status: 'active' }
        step.items.push(item)
      }
      if (event.type === 'hypothesis.recorded')
        item.statement = str(p, 'statement') ?? item.statement
      else item.status = str(p, 'status') ?? item.status
      return
    }
    case 'finding.recorded': {
      const ref = str(p, 'ref')
      if (!event.assignment_id || !ref) return
      const step = assignmentStep(turn, event.assignment_id, at)
      if (step.items.some((i) => i.kind === 'finding' && i.ref === ref)) return
      step.items.push({
        kind: 'finding',
        key: ref,
        ref,
        title: str(p, 'title'),
        status: str(p, 'status'),
      })
      return
    }
    case 'budget.exhausted': {
      const scope = str(p, 'scope')
      if (scope === 'local' && event.assignment_id) {
        assignmentStep(turn, event.assignment_id, at).budget = str(p, 'reason') ?? 'budget'
      } else {
        notice(
          turn,
          `budget:${scope}`,
          'Investigation budget reached; the answer may be limited',
          'warning',
          at,
        )
      }
      return
    }
    case 'report.revised': {
      const version = num(p, 'version')
      if (version !== null)
        notice(turn, `report:${version}`, `Report version ${version} recorded`, 'success', at)
      return
    }
    case 'turn.completed':
      turn.finished = 'completed'
      notice(turn, 'finished', 'Answer ready', 'success', at)
      return
    case 'turn.failed':
      turn.finished = 'failed'
      notice(turn, 'finished', 'Turn failed', 'danger', at)
      return
    case 'turn.cancelled':
      turn.finished = 'cancelled'
      notice(turn, 'finished', 'Turn cancelled', 'warning', at)
      return
  }
}

export function buildActivity(events: readonly ProgressEvent[]): Map<string, TurnActivity> {
  const turns = new Map<string, TurnActivity>()
  for (const event of events) {
    if (!event.turn_id) continue
    let turn = turns.get(event.turn_id)
    if (!turn) {
      turn = emptyTurn(event.turn_id, event.created_at)
      turns.set(event.turn_id, turn)
    }
    apply(turn, event)
  }
  for (const turn of turns.values()) {
    const tools = turn.steps.flatMap((s) => (s.kind === 'assignment' ? s.items : []))
    turn.toolCalls = tools.filter((i) => i.kind === 'tool').length
    turn.evidence = new Set(tools.flatMap((i) => (i.kind === 'tool' ? i.evidence : []))).size
    turn.findings = tools.filter((i) => i.kind === 'finding').length
  }
  return turns
}

export function activitySummary(activity: TurnActivity) {
  const parts = [`${activity.toolCalls} tool call${activity.toolCalls === 1 ? '' : 's'}`]
  if (activity.evidence) parts.push(`${activity.evidence} evidence`)
  if (activity.findings)
    parts.push(`${activity.findings} finding${activity.findings === 1 ? '' : 's'}`)
  return parts.join(' · ')
}
