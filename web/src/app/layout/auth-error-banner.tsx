import { ShieldAlert, X } from 'lucide-react'
import { useSearchParams } from 'react-router'

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'

// The backend's OIDC callback redirects here with ?auth_error=<code> when sign-in fails.
// Only known codes are explained; the value is never rendered as given.
const MESSAGES: Record<string, string> = {
  provider_error: 'The sign-in provider refused or cancelled the sign-in.',
  invalid_state: 'The sign-in attempt expired or was started in another browser. Try again.',
  account_disabled: 'This account is disabled. Ask the person who runs Inquest.',
  // The same message whatever the reason (D042): nothing about other accounts or invitations.
  not_admitted:
    'You signed in, but this account is not approved to use Inquest. Ask the person who runs Inquest for access. If you were invited, open the invitation link again and sign in with the email address it was sent to.',
  provider_unavailable: 'The sign-in provider could not be reached. Try again later.',
}
const FALLBACK = 'The sign-in response could not be verified, so you were not signed in.'

export function AuthErrorBanner() {
  const [params, setParams] = useSearchParams()
  const code = params.get('auth_error')
  if (!code) return null
  return (
    <Alert variant="destructive" className="mb-4">
      <ShieldAlert aria-hidden />
      <AlertTitle>Sign-in failed</AlertTitle>
      <AlertDescription className="flex items-start justify-between gap-2">
        <span>{MESSAGES[code] ?? FALLBACK}</span>
        <Button
          variant="ghost"
          size="icon"
          className="-mt-1 size-7"
          aria-label="Dismiss"
          onClick={() =>
            setParams(
              (p) => {
                p.delete('auth_error')
                return p
              },
              { replace: true },
            )
          }
        >
          <X aria-hidden />
        </Button>
      </AlertDescription>
    </Alert>
  )
}
