import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  keepPreviousData,
} from '@tanstack/react-query'

import {
  askFollowUp,
  cancelTurn,
  createInvestigation,
  getCitation,
  getInvestigation,
  getPresentation,
  getReport,
  listInvestigations,
  queryKeys,
  startInvestigation,
  type InvestigationCreate,
} from '@/lib/api'

const PAGE_SIZE = 20

export function useInvestigation(id: string, { enabled = true } = {}) {
  return useQuery({
    queryKey: queryKeys.investigation(id),
    queryFn: ({ signal }) => getInvestigation(id, { signal }),
    enabled,
  })
}

export function useInvestigationList(pageSize = PAGE_SIZE) {
  return useInfiniteQuery({
    queryKey: [...queryKeys.investigations, pageSize],
    queryFn: ({ pageParam, signal }) =>
      listInvestigations({ limit: pageSize, offset: pageParam }, { signal }),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
  })
}

export function useReport(id: string, version: number, { enabled = true } = {}) {
  return useQuery({
    queryKey: queryKeys.report(id, version),
    queryFn: ({ signal }) => getReport(id, version, { signal }),
    // Report versions are immutable.
    staleTime: Infinity,
    enabled,
  })
}

// Stored server-side and derived from immutable data: fetched once per turn.
export function usePresentation(id: string, turnId: string, { enabled = true } = {}) {
  return useQuery({
    queryKey: queryKeys.presentation(id, turnId),
    queryFn: ({ signal }) => getPresentation(id, turnId, { signal }),
    staleTime: Infinity,
    // The client default: transient failures retry, contract errors (unknown version)
    // fall back to the Markdown answer at once.
    enabled,
  })
}

export function useCitation(id: string, ref: string | null) {
  return useQuery({
    queryKey: queryKeys.citation(id, ref ?? ''),
    queryFn: ({ signal }) => getCitation(id, ref!, { signal }),
    enabled: ref !== null,
    // Evidence, findings and hypothesis statements do not change once recorded; a
    // hypothesis status can, so do not cache forever.
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  })
}

function useRefreshInvestigation(id: string) {
  const client = useQueryClient()
  return () =>
    Promise.all([
      client.invalidateQueries({ queryKey: queryKeys.investigation(id) }),
      client.invalidateQueries({ queryKey: queryKeys.investigations }),
    ])
}

export function useStartInvestigation(id: string) {
  const refresh = useRefreshInvestigation(id)
  return useMutation({ mutationFn: () => startInvestigation(id), onSettled: refresh })
}

export function useAskFollowUp(id: string) {
  const refresh = useRefreshInvestigation(id)
  return useMutation({
    mutationFn: (question: string) => askFollowUp(id, { question }),
    onSettled: refresh,
  })
}

export function useCancelTurn(id: string) {
  const refresh = useRefreshInvestigation(id)
  return useMutation({ mutationFn: () => cancelTurn(id), onSettled: refresh })
}

// Create, then start. A failed start (e.g. 503 when the runner is disabled) leaves a
// pending investigation that can be started later from its page, so it is reported
// separately instead of failing the whole flow.
export function useCreateInvestigation() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: async (data: InvestigationCreate) => {
      const investigation = await createInvestigation(data)
      try {
        await startInvestigation(investigation.id)
        return { investigation, startError: null }
      } catch (startError) {
        return { investigation, startError }
      }
    },
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.investigations }),
  })
}
