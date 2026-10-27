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
  organization_id: id.nullable(),
  owner_user_id: id.nullable(),
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
  organization_id: id.nullable(),
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

export const investigationListItem = z.object({
  id,
  title: z.string(),
  status: investigationStatus,
  severity: severity.nullable(),
  trigger_type: triggerType,
  created_at: timestamp,
  updated_at: timestamp,
  started_at: timestamp.nullable(),
  completed_at: timestamp.nullable(),
  environments: z.array(environmentSummary),
  turn_count: z.int(),
  latest_report_version: z.int().nullable(),
  latest_turn_status: turnStatus.nullable(),
  latest_turn_completed_at: timestamp.nullable(),
})

export const investigationPage = z.object({
  items: z.array(investigationListItem),
  next_offset: z.int().nullable(),
})

export const environmentListItem = environmentSummary.extend({
  created_at: timestamp,
  investigation_count: z.int(),
  last_investigation_at: timestamp.nullable(),
})

export const environmentList = z.array(environmentListItem)

// GET /environments/{id} (src/schemas/environment.py EnvironmentDetail).
const providerStatus = z.object({
  provider: z.string(),
  // process_environment: the backend's development settings (operator grant only);
  // environment: this environment's own credentials, which arrive in Phase 7C.
  source: z.enum(['process_environment', 'environment']),
  configured: z.boolean(),
  issues: z.array(z.string()),
})

export const environmentDetail = environmentSummary.extend({
  created_at: timestamp,
  updated_at: timestamp,
  organization_id: id.nullable(),
  owner_user_id: id.nullable(),
  credentials: z.enum(['process_development', 'configuration_required']),
  scope: z.object({
    status: z.enum(['configured', 'missing', 'invalid']),
    hosts: z.array(z.string()),
    services: z.array(z.string()),
    deployment_environment: z.string().nullable(),
    problems: z.array(z.object({ code: z.string(), message: z.string() })),
  }),
  stats: z.object({
    investigations: z.int(),
    active_investigations: z.int(),
    evidence: z.int(),
    tool_calls: z.int(),
    last_investigation_at: timestamp.nullable(),
  }),
  telemetry_provider: providerStatus,
  ai_provider: providerStatus.extend({
    main_model: z.string().nullable(),
    local_model: z.string().nullable(),
  }),
  per_environment_credentials: z.literal('not_available'),
})

// GET /environments/{id}/telemetry: recorded by investigations, not a live check.
export const telemetryCoverage = z.object({
  signals: z.array(
    z.object({
      signal: z.enum(['metrics', 'logs', 'traces']),
      identity: z.string(),
      value: z.string(),
      status: z.enum(['observed', 'no_data', 'unknown']),
      window_start: timestamp.nullable(),
      window_end: timestamp.nullable(),
      checked_at: timestamp,
      investigation_id: id,
    }),
  ),
  queries_checked: z.int(),
  queries_failed: z.int(),
})

// GET /investigations/{id}/citations/{ref}: recorded summaries and metadata only.
const citationBase = { ref: z.string(), environment: environmentSummary }

export const evidenceCitation = z.object({
  ...citationBase,
  kind: z.literal('evidence'),
  evidence_type: z.string(),
  title: z.string(),
  summary: z.string(),
  source_provider: z.string(),
  tool_name: z.string().nullable(),
  entity_type: z.string().nullable(),
  entity_identifier: z.string().nullable(),
  observed_at: timestamp.nullable(),
  collected_at: timestamp,
  window_start: timestamp.nullable(),
  window_end: timestamp.nullable(),
  redaction_applied: z.boolean(),
})

export const findingCitation = z.object({
  ...citationBase,
  kind: z.literal('finding'),
  title: z.string(),
  summary: z.string(),
  status: z.string(),
  confidence: z.number().nullable(),
  created_at: timestamp,
  evidence: z.array(z.object({ ref: z.string(), stance: z.string(), title: z.string() })),
})

export const hypothesisCitation = z.object({
  ...citationBase,
  kind: z.literal('hypothesis'),
  statement: z.string(),
  status: z.string(),
  confidence: z.number().nullable(),
  created_at: timestamp,
})

export const citation = z.discriminatedUnion('kind', [
  evidenceCitation,
  findingCitation,
  hypothesisCitation,
])

// GET /overview: counts of stored records only (no health, alerts or uptime).
export const overviewCounts = z.object({
  investigations_total: z.int(),
  investigations_by_status: z.record(investigationStatus, z.int()),
  latest_turn_by_status: z.record(turnStatus, z.int()),
  turns_total: z.int(),
  turns_by_status: z.record(turnStatus, z.int()),
  environments_total: z.int(),
  environments_active: z.int(),
  reports_total: z.int(),
  investigations_with_reports: z.int(),
})

export const reportOutcome = z.enum(['root_cause_identified', 'probable_cause', 'inconclusive'])

// GET /reports: report versions across investigations (metadata only).
export const reportListItem = z.object({
  id,
  investigation_id: id,
  investigation_title: z.string(),
  investigation_status: investigationStatus,
  environments: z.array(environmentSummary),
  version: z.int(),
  is_latest: z.boolean(),
  turn_id: id,
  turn_seq: z.int(),
  created_at: timestamp,
  outcome: reportOutcome.nullable(),
  conclusion_count: z.int(),
})

export const reportListPage = z.object({
  items: z.array(reportListItem),
  next_offset: z.int().nullable(),
})

// Request bodies (src/schemas/*.py).
export const environmentCreate = z.object({
  // Selects one of the caller's organizations; the server checks membership.
  organization_id: id,
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
export type EnvironmentListItem = z.infer<typeof environmentListItem>
export type EnvironmentDetail = z.infer<typeof environmentDetail>
export type TelemetryCoverage = z.infer<typeof telemetryCoverage>
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
export type InvestigationListItem = z.infer<typeof investigationListItem>
export type InvestigationPage = z.infer<typeof investigationPage>
export type EvidenceCitation = z.infer<typeof evidenceCitation>
export type FindingCitation = z.infer<typeof findingCitation>
export type HypothesisCitation = z.infer<typeof hypothesisCitation>
export type Citation = z.infer<typeof citation>
export type OverviewCounts = z.infer<typeof overviewCounts>
export type ReportOutcome = z.infer<typeof reportOutcome>
export type ReportListItem = z.infer<typeof reportListItem>
export type ReportListPage = z.infer<typeof reportListPage>
