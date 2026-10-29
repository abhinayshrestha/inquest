import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ME_KEY } from '@/app/contexts/organization'
import {
  acceptInvitation,
  changeRole,
  createInvitation,
  createOrganization,
  getOrganization,
  listInvitations,
  listMembers,
  removeMember,
  revokeInvitation,
  type InviteInput,
  type OrganizationRole,
} from '@/lib/api/organizations'

const key = (orgId: string, part?: string) =>
  part ? ['organization', orgId, part] : ['organization', orgId]

export function useOrganizationDetail(orgId: string) {
  return useQuery({ queryKey: key(orgId), queryFn: () => getOrganization(orgId) })
}

export function useMembers(orgId: string) {
  return useQuery({ queryKey: key(orgId, 'members'), queryFn: () => listMembers(orgId) })
}

export function useInvitations(orgId: string, { enabled = true } = {}) {
  return useQuery({
    queryKey: key(orgId, 'invitations'),
    queryFn: () => listInvitations(orgId),
    enabled,
  })
}

function useRefresh(orgId?: string) {
  const client = useQueryClient()
  return () =>
    Promise.all([
      client.invalidateQueries({ queryKey: ME_KEY }),
      orgId ? client.invalidateQueries({ queryKey: key(orgId) }) : Promise.resolve(),
    ])
}

export function useCreateOrganization() {
  const refresh = useRefresh()
  return useMutation({ mutationFn: createOrganization, onSuccess: refresh })
}

export function useChangeRole(orgId: string) {
  const refresh = useRefresh(orgId)
  return useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: OrganizationRole }) =>
      changeRole(orgId, userId, role),
    onSettled: refresh,
  })
}

export function useRemoveMember(orgId: string) {
  const refresh = useRefresh(orgId)
  return useMutation({
    mutationFn: (userId: string) => removeMember(orgId, userId),
    onSettled: refresh,
  })
}

export function useCreateInvitation(orgId: string) {
  const refresh = useRefresh(orgId)
  return useMutation({
    mutationFn: (input: InviteInput) => createInvitation(orgId, input),
    onSettled: refresh,
  })
}

export function useRevokeInvitation(orgId: string) {
  const refresh = useRefresh(orgId)
  return useMutation({
    mutationFn: (invitationId: string) => revokeInvitation(orgId, invitationId),
    onSettled: refresh,
  })
}

export function useAcceptInvitation() {
  const refresh = useRefresh()
  return useMutation({ mutationFn: acceptInvitation, onSuccess: refresh })
}
