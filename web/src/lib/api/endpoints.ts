import { API_BASE, request } from './client'
import { presentationEnvelope } from './presentation'
import {
  citation,
  environmentDetail,
  environmentList,
  environmentRead,
  healthResponse,
  investigationPage,
  investigationRead,
  overviewCounts,
  reportListPage,
  reportRead,
  telemetryCoverage,
  turnAccepted,
  turnCancelled,
  type EnvironmentCreate,
  type InvestigationStatus,
  type ReportOutcome,
  type TurnStatus,
  type FollowUpCreate,
  type InvestigationCreate,
} from './schemas'

// One function per existing FastAPI route (src/api/routes/). Collections take the selected
// organization; the backend checks the session's membership on every request (D033), so
// this is a selector, never an authorization input. Credential writes do not exist (7C).

type Options = { signal?: AbortSignal }

export function getHealth({ signal }: Options = {}) {
  return request('/health', healthResponse, { signal })
}

function inOrganization(organizationId: string, params = new URLSearchParams()) {
  params.set('organization_id', organizationId)
  return params
}

export function listEnvironments(organizationId: string, { signal }: Options = {}) {
  return request(`/environments?${inOrganization(organizationId)}`, environmentList, { signal })
}

export function getEnvironment(id: string, { signal }: Options = {}) {
  return request(`/environments/${encodeURIComponent(id)}`, environmentDetail, { signal })
}

export function getTelemetryCoverage(id: string, { signal }: Options = {}) {
  return request(`/environments/${encodeURIComponent(id)}/telemetry`, telemetryCoverage, {
    signal,
  })
}

export function createEnvironment(data: EnvironmentCreate) {
  return request('/environments', environmentRead, { method: 'POST', json: data })
}

export function createInvestigation(data: InvestigationCreate) {
  return request('/investigations', investigationRead, { method: 'POST', json: data })
}

export type InvestigationFilters = {
  limit?: number
  offset?: number
  environmentId?: string
  statuses?: readonly InvestigationStatus[]
  latestTurnStatuses?: readonly TurnStatus[]
}

export function listInvestigations(
  organizationId: string,
  {
    limit = 20,
    offset = 0,
    environmentId,
    statuses = [],
    latestTurnStatuses = [],
  }: InvestigationFilters = {},
  { signal }: Options = {},
) {
  const params = inOrganization(
    organizationId,
    new URLSearchParams({ limit: String(limit), offset: String(offset) }),
  )
  if (environmentId) params.set('environment_id', environmentId)
  for (const s of statuses) params.append('status', s)
  for (const s of latestTurnStatuses) params.append('latest_turn_status', s)
  return request(`/investigations?${params}`, investigationPage, { signal })
}

export function getOverview(organizationId: string, { signal }: Options = {}) {
  return request(`/overview?${inOrganization(organizationId)}`, overviewCounts, { signal })
}

export type ReportFilters = {
  limit?: number
  offset?: number
  outcome?: ReportOutcome
  environmentId?: string
  latestOnly?: boolean
  q?: string
}

export function listReports(
  organizationId: string,
  { limit = 20, offset = 0, outcome, environmentId, latestOnly, q }: ReportFilters = {},
  { signal }: Options = {},
) {
  const params = inOrganization(
    organizationId,
    new URLSearchParams({ limit: String(limit), offset: String(offset) }),
  )
  if (outcome) params.set('outcome', outcome)
  if (environmentId) params.set('environment_id', environmentId)
  if (latestOnly) params.set('latest_only', 'true')
  if (q?.trim()) params.set('q', q.trim().slice(0, 100))
  return request(`/reports?${params}`, reportListPage, { signal })
}

export function getInvestigation(id: string, { signal }: Options = {}) {
  return request(`/investigations/${encodeURIComponent(id)}`, investigationRead, { signal })
}

export function startInvestigation(id: string) {
  return request(`/investigations/${encodeURIComponent(id)}/start`, turnAccepted, {
    method: 'POST',
  })
}

export function askFollowUp(id: string, data: FollowUpCreate) {
  return request(`/investigations/${encodeURIComponent(id)}/turns`, turnAccepted, {
    method: 'POST',
    json: data,
  })
}

export function cancelTurn(id: string) {
  return request(`/investigations/${encodeURIComponent(id)}/cancel`, turnCancelled, {
    method: 'POST',
  })
}

export function getReport(id: string, version: number, { signal }: Options = {}) {
  return request(`/investigations/${encodeURIComponent(id)}/reports/${version}`, reportRead, {
    signal,
  })
}

export function getCitation(id: string, ref: string, { signal }: Options = {}) {
  return request(
    `/investigations/${encodeURIComponent(id)}/citations/${encodeURIComponent(ref)}`,
    citation,
    { signal },
  )
}

// Response blocks of a completed turn: stored server-side, built on first request from the
// validated answer and its evidence (no model call). Blocks are validated one by one when
// rendered (lib/api/presentation.ts).
export function getPresentation(id: string, turnId: string, { signal }: Options = {}) {
  return request(
    `/investigations/${encodeURIComponent(id)}/turns/${encodeURIComponent(turnId)}/presentation`,
    presentationEnvelope,
    { signal },
  )
}

// For EventSource. The backend's TurnAccepted.events_url is relative to the
// backend root, so browser code always builds the URL here instead of using it directly.
export function eventsUrl(id: string, after?: number) {
  const query = after === undefined ? '' : `?after=${after}`
  return `${API_BASE}/investigations/${encodeURIComponent(id)}/events${query}`
}
