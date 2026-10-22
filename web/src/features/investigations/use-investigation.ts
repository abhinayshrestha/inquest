import { useQuery } from '@tanstack/react-query'

import { getInvestigation, queryKeys } from '@/lib/api'

export function useInvestigation(id: string, { enabled = true } = {}) {
  return useQuery({
    queryKey: queryKeys.investigation(id),
    queryFn: ({ signal }) => getInvestigation(id, { signal }),
    enabled,
  })
}
