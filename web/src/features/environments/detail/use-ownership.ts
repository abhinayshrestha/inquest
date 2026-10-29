import { useAuth } from '@/app/contexts/auth'
import { useMembers } from '@/features/organizations/queries'
import type { EnvironmentDetail } from '@/lib/api'

// Who owns the environment and how to name people, from the organization's member list.
// Display only: the server decides who may do what.
export function useOwnership(environment: EnvironmentDetail) {
  const { user } = useAuth()
  const members = useMembers(environment.organization_id ?? '')
  const isOwner = environment.owner_user_id !== null && environment.owner_user_id === user?.id
  const nameOf = (userId: string | null) => {
    if (userId === null) return 'nobody'
    if (userId === user?.id) return 'you'
    const member = members.data?.find((m) => m.user_id === userId)
    return member ? member.display_name || member.email || 'a member' : 'a former member'
  }
  return { isOwner, ownerName: nameOf(environment.owner_user_id), nameOf, members }
}
