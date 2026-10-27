import type { z } from 'zod'

// The Vite dev server proxies /api/* to the FastAPI backend (vite.config.ts), so the
// browser never needs the backend address or CORS.
export const API_BASE = '/api'

export type ApiErrorKind =
  | 'network'
  | 'unauthorized'
  | 'forbidden'
  | 'not_found'
  | 'conflict'
  | 'gone'
  | 'unavailable'
  | 'validation'
  | 'invalid_response'
  | 'http'

// The session's CSRF token, from GET /auth/session. Kept in memory only (never in
// storage) and sent as X-CSRF-Token on every write; the session cookie itself is
// HttpOnly and never visible to scripts.
let csrfToken: string | null = null

export function setCsrfToken(token: string | null) {
  csrfToken = token
}

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

// A class so callers can tell API failures from programming errors with instanceof.
export class ApiError extends Error {
  readonly kind: ApiErrorKind
  readonly status: number

  constructor(kind: ApiErrorKind, status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.kind = kind
    this.status = status
  }
}

function kindFor(status: number): ApiErrorKind {
  if (status === 401) return 'unauthorized'
  if (status === 403) return 'forbidden'
  if (status === 404) return 'not_found'
  if (status === 410) return 'gone'
  if (status === 409) return 'conflict'
  if (status === 422) return 'validation'
  if (status === 503) return 'unavailable'
  return 'http'
}

// FastAPI sends {"detail": "..."} for HTTPException and {"detail": [{msg, loc}, ...]}
// for request validation errors.
function detailMessage(body: unknown, status: number): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const { detail } = body as { detail: unknown }
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      const messages = detail
        .map((d) => (d && typeof d === 'object' && 'msg' in d ? String(d.msg) : null))
        .filter(Boolean)
      if (messages.length) return messages.join('; ')
    }
  }
  return `Request failed with status ${status}`
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

export async function request<S extends z.ZodType>(
  path: string,
  schema: S,
  init: RequestInit & { json?: unknown } = {},
): Promise<z.infer<S>> {
  const { json, headers, ...rest } = init
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...rest,
      headers: {
        Accept: 'application/json',
        ...(json === undefined ? {} : { 'Content-Type': 'application/json' }),
        ...(csrfToken && !SAFE_METHODS.has((rest.method ?? 'GET').toUpperCase())
          ? { 'X-CSRF-Token': csrfToken }
          : {}),
        ...headers,
      },
      body: json === undefined ? rest.body : JSON.stringify(json),
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError('network', 0, 'Could not reach the Inquest backend')
  }

  const body = await readJson(response)
  if (!response.ok) {
    throw new ApiError(
      kindFor(response.status),
      response.status,
      detailMessage(body, response.status),
    )
  }
  const parsed = schema.safeParse(body)
  if (!parsed.success) {
    // Shape mismatches mean the frontend and backend contracts disagree; surface it
    // instead of rendering half-valid data.
    if (import.meta.env.DEV) console.error('Unexpected API response', path, parsed.error.issues)
    throw new ApiError(
      'invalid_response',
      response.status,
      'The backend returned an unexpected response',
    )
  }
  return parsed.data
}
