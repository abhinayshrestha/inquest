import { useInfiniteQuery } from '@tanstack/react-query'

import { useOrganizationId } from '@/app/contexts/organization'
import { listReports, queryKeys, type ReportFilters } from '@/lib/api'

const PAGE_SIZE = 20

export function useReportList(filters: Omit<ReportFilters, 'limit' | 'offset'>) {
  const organizationId = useOrganizationId()
  return useInfiniteQuery({
    queryKey: [...queryKeys.reports(organizationId ?? ''), filters],
    queryFn: ({ pageParam, signal }) =>
      listReports(organizationId!, { ...filters, limit: PAGE_SIZE, offset: pageParam }, { signal }),
    enabled: organizationId !== null,
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
  })
}
