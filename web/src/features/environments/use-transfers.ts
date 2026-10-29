import { useMutation, useQuery } from '@tanstack/react-query'

import { useAuth } from '@/app/contexts/auth'
import { listTransfers, proposeTransfer, resolveTransfer, TRANSFERS_KEY } from '@/lib/api/transfers'

import { useRefreshEnvironment } from './use-credentials'

// Pending transfers to or from the signed-in user, across their organizations. Re-read
// every minute so an incoming offer shows up without a reload.
export function useTransfers() {
  const { status } = useAuth()
  return useQuery({
    queryKey: TRANSFERS_KEY,
    queryFn: ({ signal }) => listTransfers({ signal }),
    enabled: status === 'signed_in',
    refetchInterval: 60_000,
  })
}

export function useProposeTransfer(environmentId: string) {
  const refresh = useRefreshEnvironment(environmentId)
  return useMutation({
    mutationFn: (toUserId: string) => proposeTransfer(environmentId, toUserId),
    onSettled: refresh,
  })
}

export function useResolveTransfer(environmentId: string) {
  const refresh = useRefreshEnvironment(environmentId)
  return useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'accept' | 'decline' | 'cancel' }) =>
      resolveTransfer(id, action),
    onSettled: refresh,
  })
}
