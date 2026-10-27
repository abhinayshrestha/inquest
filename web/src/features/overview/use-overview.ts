import { useQuery } from '@tanstack/react-query'

import { useOrganizationId } from '@/app/contexts/organization'
import { getOverview, queryKeys } from '@/lib/api'

export function useOverview() {
  const organizationId = useOrganizationId()
  return useQuery({
    queryKey: queryKeys.overview(organizationId ?? ''),
    queryFn: ({ signal }) => getOverview(organizationId!, { signal }),
    enabled: organizationId !== null,
  })
}
