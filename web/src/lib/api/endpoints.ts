import { API_BASE, request } from './client'
import {
  environmentRead,
  healthResponse,
  investigationRead,
  reportRead,
  turnAccepted,
  turnCancelled,
  type EnvironmentCreate,
  type FollowUpCreate,
  type InvestigationCreate,
} from './schemas'

// One function per existing FastAPI route (src/api/routes/). Endpoints the backend does
// not have (environment list/read, investigation list, evidence, organizations) are
// deliberately absent; pages show "not available yet" states instead.

type Options = { signal?: AbortSignal }

export function getHealth({ signal }: Options = {}) {
  return request('/health', healthResponse, { signal })
}

export function createEnvironment(data: EnvironmentCreate) {
  return request('/environments', environmentRead, { method: 'POST', json: data })
}

export function createInvestigation(data: InvestigationCreate) {
  return request('/investigations', investigationRead, { method: 'POST', json: data })
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

// For EventSource (Phase 6B). The backend's TurnAccepted.events_url is relative to the
// backend root, so browser code always builds the URL here instead of using it directly.
export function eventsUrl(id: string, after?: number) {
  const query = after === undefined ? '' : `?after=${after}`
  return `${API_BASE}/investigations/${encodeURIComponent(id)}/events${query}`
}
