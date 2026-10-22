import { useQuery } from '@tanstack/react-query'

import { getHealth, queryKeys } from '@/lib/api'

export function useHealth() {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: ({ signal }) => getHealth({ signal }),
    refetchInterval: 30_000,
  })
}
