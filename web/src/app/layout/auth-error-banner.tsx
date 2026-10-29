import { ShieldAlert, X } from 'lucide-react'
import { useSearchParams } from 'react-router'

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { authErrorMessage } from '@/features/auth/auth-errors'

export function AuthErrorBanner() {
  const [params, setParams] = useSearchParams()
  const code = params.get('auth_error')
  if (!code) return null
  return (
    <Alert variant="destructive" className="mb-4">
      <ShieldAlert aria-hidden />
      <AlertTitle>Sign-in failed</AlertTitle>
      <AlertDescription className="flex items-start justify-between gap-2">
        <span>{authErrorMessage(code)}</span>
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
