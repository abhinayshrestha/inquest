import { describe, expect, it } from 'vitest'

import { investigation, INVESTIGATION_ID } from '@/test/fixtures'
import { mockFetch } from '@/test/render'

import { ApiError } from './client'
import {
  askFollowUp,
  eventsUrl,
  getHealth,
  getInvestigation,
  startInvestigation,
} from './endpoints'

async function apiError(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise
  } catch (error) {
    expect(error).toBeInstanceOf(ApiError)
    return error as ApiError
  }
  throw new Error('expected an ApiError')
}

describe('api client', () => {
  it('calls the backend through the /api proxy prefix and validates the response', async () => {
    const fetchMock = mockFetch({ 'GET /health': { body: { status: 'ok', database: 'ok' } } })
    await expect(getHealth()).resolves.toEqual({ status: 'ok', database: 'ok' })
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/health')
  })

  it('parses an investigation with Pydantic timestamp formats', async () => {
    mockFetch({ [`GET /investigations/${INVESTIGATION_ID}`]: { body: investigation } })
    const result = await getInvestigation(INVESTIGATION_ID)
    expect(result.turns[0]?.decision).toBe('investigated')
    expect(result.reports[0]?.version).toBe(1)
  })

  it('sends JSON bodies for follow-ups', async () => {
    const accepted = {
      investigation_id: INVESTIGATION_ID,
      turn_id: investigation.turns[0]!.id,
      seq: 2,
      status: 'queued',
      events_url: `/investigations/${INVESTIGATION_ID}/events`,
    }
    const fetchMock = mockFetch({
      [`POST /investigations/${INVESTIGATION_ID}/turns`]: { status: 202, body: accepted },
    })
    await expect(askFollowUp(INVESTIGATION_ID, { question: 'Still slow?' })).resolves.toEqual(
      accepted,
    )
    const init = fetchMock.mock.calls[0]?.[1]
    expect(init?.body).toBe(JSON.stringify({ question: 'Still slow?' }))
    expect(new Headers(init?.headers).get('Content-Type')).toBe('application/json')
  })

  it.each([
    [404, 'not_found', 'investigation not found'],
    [409, 'conflict', 'turn already active'],
    [503, 'unavailable', 'investigation runner is disabled'],
  ] as const)('maps HTTP %i to %s with the FastAPI detail', async (status, kind, detail) => {
    mockFetch({ [`POST /investigations/${INVESTIGATION_ID}/start`]: { status, body: { detail } } })
    const error = await apiError(startInvestigation(INVESTIGATION_ID))
    expect(error.kind).toBe(kind)
    expect(error.status).toBe(status)
    expect(error.message).toBe(detail)
  })

  it('joins FastAPI validation error messages', async () => {
    mockFetch({
      [`POST /investigations/${INVESTIGATION_ID}/turns`]: {
        status: 422,
        body: {
          detail: [{ loc: ['body', 'question'], msg: 'String should have at least 1 character' }],
        },
      },
    })
    const error = await apiError(askFollowUp(INVESTIGATION_ID, { question: '' }))
    expect(error.kind).toBe('validation')
    expect(error.message).toBe('String should have at least 1 character')
  })

  it('rejects responses that do not match the contract', async () => {
    mockFetch({ 'GET /health': { body: { status: 'ok' } } })
    const error = await apiError(getHealth())
    expect(error.kind).toBe('invalid_response')
  })

  it('reports network failures as their own kind', async () => {
    mockFetch({})
    const error = await apiError(getHealth())
    expect(error.kind).toBe('network')
  })

  it('builds the SSE URL under the proxy prefix', () => {
    expect(eventsUrl(INVESTIGATION_ID)).toBe(`/api/investigations/${INVESTIGATION_ID}/events`)
    expect(eventsUrl(INVESTIGATION_ID, 42)).toBe(
      `/api/investigations/${INVESTIGATION_ID}/events?after=42`,
    )
  })
})
