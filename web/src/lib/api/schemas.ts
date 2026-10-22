import { z } from 'zod'

// Mirrors the FastAPI response models in src/schemas/ and the enums in src/models/.
// Change these only after the backend contract changes; never add fields the backend
// does not send.

const timestamp = z.iso.datetime({ offset: true })
const id = z.uuid()
// answer_json / report_json are typed when the response renderer is built (Phase 6B+);
// until then they are opaque JSON objects.
const json = z.record(z.string(), z.unknown())

export const environmentType = z.enum(['local', 'aws'])
export const environmentStatus = z.enum(['active', 'disabled'])
export const investigationStatus = z.enum([
  'pending',
  'investigating',
  'correlating',
  'idle',
  'completed',
  'failed',
  'cancelled',
])
export const severity = z.enum(['low', 'medium', 'high', 'critical'])
export const triggerType = z.enum(['manual', 'alert'])
export const assignmentStatus = z.enum([
  'pending',
  'running',
  'completed',
  'failed',
  'timed_out',
  'cancelled',
])
export const turnKind = z.enum(['initial', 'follow_up'])
export const turnStatus = z.enum(['pending', 'running', 'completed', 'failed', 'cancelled'])
export const turnDecision = z.enum(['reused_evidence', 'investigated', 'clarification'])

export const healthResponse = z.object({ status: z.string(), database: z.string() })

export const environmentSummary = z.object({
  id,
  name: z.string(),
  type: environmentType,
  status: environmentStatus,
})

export const environmentRead = environmentSummary.extend({
  metadata_json: json,
  created_at: timestamp,
  updated_at: timestamp,
})

export const assignmentRead = z.object({
  id,
  turn_id: id,
  round: z.int(),
  objective: z.string(),
  status: assignmentStatus,
  started_at: timestamp.nullable(),
  completed_at: timestamp.nullable(),
  created_at: timestamp,
  updated_at: timestamp,
  environment: environmentSummary,
})

export const turnRead = z.object({
  id,
  seq: z.int(),
  kind: turnKind,
  question: z.string(),
  status: turnStatus,
  decision: turnDecision.nullable(),
  answer_json: json.nullable(),
  error_code: z.string().nullable(),
  started_at: timestamp.nullable(),
  completed_at: timestamp.nullable(),
  created_at: timestamp,
})

export const reportRef = z.object({
  id,
  turn_id: id,
  version: z.int(),
  created_at: timestamp,
})

export const reportRead = reportRef.extend({ report_json: json })

export const investigationRead = z.object({
  id,
  title: z.string(),
  question: z.string(),
  status: investigationStatus,
  severity: severity.nullable(),
  trigger_type: triggerType,
  trigger_json: json,
  current_round: z.int(),
  started_at: timestamp.nullable(),
  completed_at: timestamp.nullable(),
  final_result_json: json.nullable(),
  created_at: timestamp,
  updated_at: timestamp,
  assignments: z.array(assignmentRead),
  turns: z.array(turnRead),
  reports: z.array(reportRef),
})

export const turnAccepted = z.object({
  investigation_id: id,
  turn_id: id,
  seq: z.int(),
  status: z.string(),
  events_url: z.string(),
})

export const turnCancelled = z.object({
  investigation_id: id,
  turn_id: id,
  status: z.string(),
})

// The `data:` JSON of one SSE event (src/api/sse.py format_event). Payloads are typed per
// event type when the activity view is built.
export const progressEvent = z.object({
  id: z.int(),
  type: z.string(),
  turn_id: id.nullable(),
  assignment_id: id.nullable(),
  created_at: timestamp,
  payload: json,
})

// Request bodies (src/schemas/*.py).
export const environmentCreate = z.object({
  name: z.string().min(1).max(255),
  type: environmentType,
  metadata_json: json.optional(),
})

export const investigationCreate = z.object({
  environment_id: id,
  title: z.string().min(1).max(255),
  question: z.string().min(1),
  severity: severity.nullable().optional(),
})

export const followUpCreate = z.object({ question: z.string().min(1).max(10_000) })

export type HealthResponse = z.infer<typeof healthResponse>
export type EnvironmentType = z.infer<typeof environmentType>
export type EnvironmentStatus = z.infer<typeof environmentStatus>
export type EnvironmentSummary = z.infer<typeof environmentSummary>
export type EnvironmentRead = z.infer<typeof environmentRead>
export type InvestigationStatus = z.infer<typeof investigationStatus>
export type Severity = z.infer<typeof severity>
export type AssignmentStatus = z.infer<typeof assignmentStatus>
export type AssignmentRead = z.infer<typeof assignmentRead>
export type TurnStatus = z.infer<typeof turnStatus>
export type TurnDecision = z.infer<typeof turnDecision>
export type TurnRead = z.infer<typeof turnRead>
export type ReportRef = z.infer<typeof reportRef>
export type ReportRead = z.infer<typeof reportRead>
export type InvestigationRead = z.infer<typeof investigationRead>
export type TurnAccepted = z.infer<typeof turnAccepted>
export type TurnCancelled = z.infer<typeof turnCancelled>
export type ProgressEvent = z.infer<typeof progressEvent>
export type EnvironmentCreate = z.infer<typeof environmentCreate>
export type InvestigationCreate = z.infer<typeof investigationCreate>
export type FollowUpCreate = z.infer<typeof followUpCreate>
