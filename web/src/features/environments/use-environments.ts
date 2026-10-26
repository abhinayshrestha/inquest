import { useQuery } from '@tanstack/react-query'

import { listEnvironments, queryKeys } from '@/lib/api'

export function useEnvironments() {
  return useQuery({
    queryKey: queryKeys.environments,
    queryFn: ({ signal }) => listEnvironments({ signal }),
  })
}
