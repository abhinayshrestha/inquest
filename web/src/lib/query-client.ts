import { QueryClient } from '@tanstack/react-query'

import { ApiError } from '@/lib/api'

// Retrying cannot fix a missing resource, a conflict, missing access or a contract mismatch.
const PERMANENT = new Set([
  'not_found',
  'conflict',
  'validation',
  'invalid_response',
  'unauthorized',
  'forbidden',
  'gone',
])

export function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        refetchOnWindowFocus: false,
        retry: (failures, error) =>
          !(error instanceof ApiError && PERMANENT.has(error.kind)) && failures < 2,
      },
      mutations: { retry: false },
    },
  })
}
