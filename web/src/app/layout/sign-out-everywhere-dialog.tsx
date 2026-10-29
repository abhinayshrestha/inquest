import { Loader2 } from 'lucide-react'
import { useState } from 'react'

import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api/client'

import { useAuth } from '../contexts/auth'

type Step = 'confirm' | 'working' | 'failed' | 'done'

// Fixed text: the server's message is never shown here.
function failure(error: unknown) {
  if (error instanceof ApiError && error.status === 429)
    return 'Too many attempts. Your sessions are unchanged; try again later.'
  return 'Could not sign out everywhere. Your sessions are unchanged; try again.'
}

/** Confirms and runs sign out everywhere (D043). Once the session is gone the app moves to
 * the login page, which says that it worked. */
export function SignOutEverywhereDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { signOutEverywhere } = useAuth()
  const [step, setStep] = useState<Step>('confirm')
  const [message, setMessage] = useState('')

  const change = (next: boolean) => {
    if (step === 'working') return
    if (!next) setStep('confirm')
    onOpenChange(next)
  }
  const run = async () => {
    setStep('working')
    try {
      await signOutEverywhere()
      setStep('done')
    } catch (error) {
      setMessage(failure(error))
      setStep('failed')
    }
  }

  return (
    <AlertDialog open={open} onOpenChange={change}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            {step === 'done' ? 'Signed out everywhere' : 'Sign out everywhere?'}
          </AlertDialogTitle>
          <AlertDialogDescription>
            {step === 'done'
              ? 'Every Inquest session of this account has ended, including this one. Sign in again to continue.'
              : 'Ends your Inquest sessions in every browser, including this one, and stops their live updates. Your organizations, investigations and reports are kept.'}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {step === 'failed' && (
          <p role="alert" className="text-sm text-destructive">
            {message}
          </p>
        )}
        <AlertDialogFooter>
          {step === 'done' ? (
            <AlertDialogCancel>Close</AlertDialogCancel>
          ) : (
            <>
              <AlertDialogCancel disabled={step === 'working'}>Go back</AlertDialogCancel>
              <Button
                variant="destructive"
                onClick={() => void run()}
                disabled={step === 'working'}
              >
                {step === 'working' && <Loader2 aria-hidden className="animate-spin" />}
                Sign out everywhere
              </Button>
            </>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
