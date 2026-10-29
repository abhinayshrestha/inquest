import { Building2, Loader2, MailCheck, MailX } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'

import { useAuth } from '@/app/contexts/auth'
import { useOrganization } from '@/app/contexts/organization'
import { EmptyState } from '@/components/empty-state'
import { PageHeader } from '@/components/page-header'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api'
import { describeError } from '@/lib/errors'

import { useAcceptInvitation } from './queries'
import { SignInRequired } from './sign-in-required'

// The invitation token arrives in the URL fragment (#token=…), which browsers never send
// to servers or proxies. It is moved out of the address bar at once. When the visitor
// still has to sign in, it waits in this tab's sessionStorage for the round trip to the
// provider (single use, bound to the invited email, removed as soon as it is read).
const PENDING = 'inquest.pendingInvitation'

// Reading has no side effects (StrictMode runs state initializers twice); the effect
// below clears both places once the token is held in state.
function readToken(): string | null {
  const fromHash = new URLSearchParams(window.location.hash.slice(1)).get('token')
  if (fromHash) return fromHash
  try {
    return window.sessionStorage.getItem(PENDING)
  } catch {
    return null
  }
}

function forgetToken() {
  if (window.location.hash) {
    window.history.replaceState(window.history.state, '', window.location.pathname)
  }
  try {
    window.sessionStorage.removeItem(PENDING)
  } catch {
    // Nothing stored.
  }
}

function keepForSignIn(token: string) {
  try {
    window.sessionStorage.setItem(PENDING, token)
  } catch {
    // Without storage the visitor opens the link again after signing in.
  }
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

export default function AcceptInvitationPage() {
  const [token] = useState(readToken)
  useEffect(forgetToken, [])
  const { status, user, signInWithInvitation } = useAuth()
  const { selectOrganization } = useOrganization()
  const accept = useAcceptInvitation()
  const navigate = useNavigate()
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<unknown>(null)
  // Set by the backend when signing in accepted the invitation (a new user admitted by it,
  // D042). The server checks membership on every request; this only picks the page.
  const joined = useSearchParams()[0].get('joined')
  const joinedId = joined && UUID.test(joined) ? joined : null
  useEffect(() => {
    if (joinedId && status === 'signed_in') {
      selectOrganization(joinedId)
      navigate(`/organizations/${joinedId}`, { replace: true })
    }
  }, [joinedId, status, selectOrganization, navigate])

  let body
  if (joinedId && status !== 'signed_out') {
    body = (
      <EmptyState
        icon={Loader2}
        title="Opening your organization"
        description="Your invitation was accepted."
      />
    )
  } else if (!token) {
    body = (
      <EmptyState
        icon={MailX}
        title="No invitation in this link"
        description="Open the complete link you were sent. If it was already used or has expired, ask for a new one."
      />
    )
  } else if (status !== 'signed_in') {
    body =
      status === 'signed_out' ? (
        <EmptyState
          icon={MailCheck}
          title="Sign in to accept this invitation"
          description={
            startError
              ? describeError(startError).description
              : 'Sign in with the email address the invitation was sent to.'
          }
          action={
            <Button
              disabled={starting}
              onClick={() => {
                // Still kept for visitors who already use Inquest: they accept after
                // signing in. Others are admitted by the sign-in itself.
                keepForSignIn(token)
                setStarting(true)
                setStartError(null)
                signInWithInvitation(token, '/invitations/accept').catch((error: unknown) => {
                  setStarting(false)
                  setStartError(error)
                })
              }}
            >
              {starting && <Loader2 aria-hidden className="animate-spin" />}
              Sign in to continue
            </Button>
          }
        />
      ) : (
        <SignInRequired what="accept invitations" />
      )
  } else {
    const error = accept.error
    body = (
      <EmptyState
        icon={Building2}
        title="Join this organization?"
        description={
          error instanceof ApiError && error.kind === 'forbidden'
            ? `This invitation is for a different email address than ${user?.email ?? 'yours'}, or your email is not verified by your sign-in provider.`
            : error
              ? describeError(error).description
              : `You are signed in as ${user?.email ?? 'an account without an email'}. Accepting adds you with the role chosen by whoever invited you.`
        }
        action={
          !error ||
          !(error instanceof ApiError && ['gone', 'forbidden', 'conflict'].includes(error.kind)) ? (
            <Button
              disabled={accept.isPending}
              onClick={() =>
                accept.mutate(token, {
                  onSuccess: (membership) => {
                    selectOrganization(membership.organization_id)
                    navigate(`/organizations/${membership.organization_id}`)
                  },
                })
              }
            >
              {accept.isPending && <Loader2 aria-hidden className="animate-spin" />}
              Accept invitation
            </Button>
          ) : null
        }
      />
    )
  }
  return (
    <>
      <PageHeader title="Invitation" />
      {body}
    </>
  )
}
