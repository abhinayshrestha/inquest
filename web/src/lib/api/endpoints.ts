import { API_BASE, request } from './client'
import { presentationEnvelope } from './presentation'
import {
  citation,
  environmentList,
  environmentRead,
  healthResponse,
  investigationPage,
  investigationRead,
  reportRead,
  turnAccepted,
  turnCancelled,
  type EnvironmentCreate,
  type FollowUpCreate,
  type InvestigationCreate,
} from './schemas'

// One function per existing FastAPI route (src/api/routes/). Endpoints the backend does
// not have (environment read, organizations, ...) are deliberately absent; pages show
// "not available yet" states instead.

type Options = { signal?: AbortSignal }

export function getHealth({ signal }: Options = {}) {
  return request('/health', healthResponse, { signal })
}

export function listEnvironments({ signal }: Options = {}) {
  return request('/environments', environmentList, { signal })
}

export function createEnvironment(data: EnvironmentCreate) {
  return request('/environments', environmentRead, { method: 'POST', json: data })
}

export function createInvestigation(data: InvestigationCreate) {
  return request('/investigations', investigationRead, { method: 'POST', json: data })
}

export function listInvestigations(
  { limit = 20, offset = 0 }: { limit?: number; offset?: number } = {},
  { signal }: Options = {},
) {
  return request(`/investigations?limit=${limit}&offset=${offset}`, investigationPage, { signal })
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
