import { KeyRound, LogIn, ShieldAlert } from 'lucide-react'

import { useAuth } from '@/app/contexts/auth'
import { EmptyState } from '@/components/empty-state'
import { SectionSkeleton } from '@/components/skeletons'
import { Button } from '@/components/ui/button'

// What to show instead of organization content when nobody is signed in. With sign-in
// unconfigured there is no fallback: organization features stay unavailable.
export function SignInRequired({ returnTo, what }: { returnTo?: string; what: string }) {
  const { status, signIn } = useAuth()
  if (status === 'loading') return <SectionSkeleton label="Checking sign-in" />
  if (status === 'unconfigured' || status === 'unavailable') {
    return (
      <EmptyState
        icon={ShieldAlert}
        badge="Sign-in not configured"
        title="Organizations need sign-in"
        description="This server has no identity provider configured (INQUEST_AUTH_* settings), so nobody can sign in and organization features are unavailable. There is deliberately no fallback identity."
      />
    )
  }
  return (
    <EmptyState
      icon={KeyRound}
      title="Sign in required"
      description={`Sign in to ${what}.`}
      action={
        <Button onClick={() => signIn(returnTo)}>
          <LogIn aria-hidden />
          Sign in
        </Button>
      }
    />
  )
}
