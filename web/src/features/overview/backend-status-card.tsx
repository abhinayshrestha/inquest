import { Activity, CheckCircle2, XCircle } from 'lucide-react'

import { Spinner } from '@/components/loading'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { describeError } from '@/lib/errors'

import { useHealth } from './use-health'

export function BackendStatusCard() {
  const { data, error, isPending, refetch, isFetching } = useHealth()
  const healthy = data?.status === 'ok' && data.database === 'ok'

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Activity aria-hidden className="size-4" />
          Backend
        </CardTitle>
        <CardDescription>FastAPI and PostgreSQL reachability (GET /health).</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3" aria-live="polite">
        {isPending ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner label="Checking backend" /> Checking…
          </div>
        ) : error ? (
          <div className="space-y-2">
            <p className="flex items-center gap-2 text-sm font-medium text-destructive">
              <XCircle aria-hidden className="size-4" />
              {describeError(error).title}
            </p>
            <p className="text-sm text-muted-foreground">{describeError(error).description}</p>
            <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching}>
              Retry
            </Button>
          </div>
        ) : (
          <p className="flex items-center gap-2 text-sm font-medium">
            {healthy ? (
              <CheckCircle2 aria-hidden className="size-4 text-success" />
            ) : (
              <XCircle aria-hidden className="size-4 text-warning" />
            )}
            API {data.status} · database {data.database}
          </p>
        )}
      </CardContent>
    </Card>
  )
}
