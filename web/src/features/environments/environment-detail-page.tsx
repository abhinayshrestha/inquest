import { ArrowLeft, Server } from 'lucide-react'
import { Link, useParams, useSearchParams } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { PageHeader } from '@/components/page-header'
import { PageSkeleton } from '@/components/skeletons'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { ApiError } from '@/lib/api'
import { isUuid } from '@/lib/ids'
import { InOrganization } from '@/features/organizations/organization-gate'

import { AiProviderTab } from './detail/ai-provider-tab'
import { OverviewTab } from './detail/overview-tab'
import { SettingsTab } from './detail/settings-tab'
import { TelemetryTab } from './detail/telemetry-tab'
import { useEnvironment } from './use-environments'

const TABS = [
  ['overview', 'Overview'],
  ['telemetry', 'Telemetry'],
  ['ai', 'AI provider'],
  ['settings', 'Settings'],
] as const
type Tab = (typeof TABS)[number][0]

function BackLink() {
  return (
    <Button asChild variant="ghost" size="sm" className="mb-2 -ml-2">
      <Link to="/environments">
        <ArrowLeft aria-hidden />
        Environments
      </Link>
    </Button>
  )
}

function NotFound() {
  return (
    <>
      <BackLink />
      <EmptyState
        icon={Server}
        title="Environment not found"
        description="It does not exist, or it belongs to an organization you are not a member of."
      />
    </>
  )
}

export default function EnvironmentDetailPage() {
  const { id } = useParams()
  const valid = isUuid(id)
  const query = useEnvironment(id ?? '', { enabled: valid })
  // The section lives in the URL so it survives reload and can be linked.
  const [params, setParams] = useSearchParams()
  const requested = params.get('tab')
  const tab: Tab = TABS.some(([key]) => key === requested) ? (requested as Tab) : 'overview'

  if (!valid) return <NotFound />
  if (query.isPending) return <PageSkeleton variant="detail" />
  if (query.error) {
    if (query.error instanceof ApiError && query.error.kind === 'not_found') return <NotFound />
    return (
      <>
        <BackLink />
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
      </>
    )
  }

  const environment = query.data
  return (
    <InOrganization organizationId={environment.organization_id}>
      <BackLink />
      <PageHeader
        title={environment.name}
        description={
          <span className="inline-flex flex-wrap items-center gap-2">
            <Badge variant="outline">{environment.type === 'aws' ? 'AWS' : 'Local'}</Badge>
            <Badge variant={environment.status === 'active' ? 'secondary' : 'outline'}>
              {environment.status}
            </Badge>
            {environment.credentials === 'configuration_required' && (
              <Badge variant="outline" className="border-warning/50 text-warning">
                Configuration required
              </Badge>
            )}
          </span>
        }
      />
      <Tabs
        value={tab}
        onValueChange={(next) =>
          setParams(
            (p) => {
              if (next === 'overview') p.delete('tab')
              else p.set('tab', next)
              return p
            },
            { replace: true },
          )
        }
      >
        <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
          <TabsList aria-label="Environment sections">
            {TABS.map(([key, label]) => (
              <TabsTrigger key={key} value={key}>
                {label}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>
        <TabsContent value="overview" className="mt-4">
          <OverviewTab environment={environment} />
        </TabsContent>
        <TabsContent value="telemetry" className="mt-4">
          <TelemetryTab environment={environment} />
        </TabsContent>
        <TabsContent value="ai" className="mt-4">
          <AiProviderTab environment={environment} />
        </TabsContent>
        <TabsContent value="settings" className="mt-4">
          <SettingsTab environment={environment} />
        </TabsContent>
      </Tabs>
    </InOrganization>
  )
}
