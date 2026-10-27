import { useQuery } from '@tanstack/react-query'

import { useOrganizationId } from '@/app/contexts/organization'
import { getEnvironment, getTelemetryCoverage, listEnvironments, queryKeys } from '@/lib/api'

// The selected organization's environments; idle until there is one.
export function useEnvironments() {
  const organizationId = useOrganizationId()
  return useQuery({
    queryKey: queryKeys.environments(organizationId ?? ''),
    queryFn: ({ signal }) => listEnvironments(organizationId!, { signal }),
    enabled: organizationId !== null,
  })
}

export function useEnvironment(id: string, { enabled = true } = {}) {
  return useQuery({
    queryKey: queryKeys.environment(id),
    queryFn: ({ signal }) => getEnvironment(id, { signal }),
    enabled,
  })
}

export function useTelemetryCoverage(id: string, { enabled = true } = {}) {
  return useQuery({
    queryKey: queryKeys.telemetry(id),
    queryFn: ({ signal }) => getTelemetryCoverage(id, { signal }),
    enabled,
  })
}
