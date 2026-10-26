import { QueryClient } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { vi } from 'vitest'

import { AppProviders } from '@/app/providers'
import { routes } from '@/app/routes'

export function renderRoute(path: string, { queryClient }: { queryClient?: QueryClient } = {}) {
  const client =
    queryClient ??
    new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  const result = render(
    <AppProviders queryClient={client}>
      <RouterProvider router={router} />
    </AppProviders>,
  )
  return { ...result, router, queryClient: client }
}

export type Reply = { status?: number; body?: unknown }
type Handler = Reply | Reply[] | ((request: { url: URL; body: unknown }) => Reply)

// Routes requests by "METHOD /path?query" or "METHOD /path" (without the /api prefix).
// An array is a sequence: each call takes the next reply and the last one repeats.
// Unknown requests fail loudly instead of hanging.
export function mockFetch(replies: Record<string, Handler>) {
  const calls = new Map<string, number>()
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost')
    const path = url.pathname.replace(/^\/api/, '')
    const method = init?.method ?? 'GET'
    const key = [`${method} ${path}${url.search}`, `${method} ${path}`].find((k) => k in replies)
    if (!key) throw new Error(`Unexpected request: ${method} ${path}${url.search}`)
    const handler = replies[key]!
    const n = calls.get(key) ?? 0
    calls.set(key, n + 1)
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
    const reply =
      typeof handler === 'function'
        ? handler({ url, body })
        : Array.isArray(handler)
          ? handler[Math.min(n, handler.length - 1)]!
          : handler
    return new Response(reply.body === undefined ? null : JSON.stringify(reply.body), {
      status: reply.status ?? 200,
      headers: { 'Content-Type': 'application/json' },
    })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

export function requestsTo(fetchMock: ReturnType<typeof mockFetch>, method: string, path: string) {
  return fetchMock.mock.calls.filter(([input, init]) => {
    const url = new URL(String(input), 'http://localhost')
    return (init?.method ?? 'GET') === method && url.pathname === `/api${path}`
  })
}
