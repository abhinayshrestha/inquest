import { RotateCw } from 'lucide-react'
import { isRouteErrorResponse, useRouteError } from 'react-router'

import { ErrorState } from '@/components/error-state'
import { PageHeader } from '@/components/page-header'
import { Button } from '@/components/ui/button'

import { NotFoundPage } from './not-found-page'

// A lazy page chunk can disappear after a rebuild/redeploy; only a reload fixes that.
function isChunkLoadError(error: unknown) {
  return (
    error instanceof Error &&
    /dynamically imported module|Importing a module script failed|Loading chunk/i.test(
      error.message,
    )
  )
}

export function RouteErrorPage() {
  const error = useRouteError()

  if (isRouteErrorResponse(error) && error.status === 404) return <NotFoundPage />

  if (isChunkLoadError(error)) {
    return (
      <>
        <PageHeader title="Update available" />
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">
            This page could not be loaded, usually because the app was updated. Reload to continue.
          </p>
          <Button size="sm" onClick={() => window.location.reload()}>
            <RotateCw aria-hidden />
            Reload
          </Button>
        </div>
      </>
    )
  }

  if (import.meta.env.DEV) console.error('Route error', error)
  return (
    <>
      <PageHeader title="Something went wrong" />
      <ErrorState error={error} onRetry={() => window.location.reload()} />
    </>
  )
}
