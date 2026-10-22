import { AlertTriangle, RotateCw } from 'lucide-react'

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { describeError } from '@/lib/errors'

export function ErrorState({
  error,
  onRetry,
  title,
}: {
  error: unknown
  onRetry?: () => void
  title?: string
}) {
  const described = describeError(error)
  return (
    <Alert variant="destructive" role="alert">
      <AlertTriangle aria-hidden />
      <AlertTitle>{title ?? described.title}</AlertTitle>
      <AlertDescription>
        <p>{described.description}</p>
        {onRetry && (
          <Button variant="outline" size="sm" className="mt-2" onClick={onRetry}>
            <RotateCw aria-hidden />
            Try again
          </Button>
        )}
      </AlertDescription>
    </Alert>
  )
}
