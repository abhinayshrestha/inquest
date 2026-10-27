import { Activity, AlertTriangle, CircleHelp, CircleSlash, Eye } from 'lucide-react'
import { Link } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { SectionSkeleton } from '@/components/skeletons'
import { Badge } from '@/components/ui/badge'
import type { EnvironmentDetail, TelemetryCoverage } from '@/lib/api'
import { formatDateTime, humanize } from '@/lib/format'
import { formatWindow } from '@/lib/render/blocks/format'

import { useTelemetryCoverage } from '../use-environments'
import { CredentialForm } from './credential-form'
import { PROVIDER_SOURCE } from './provider-source'
import { ConfiguredBadge, Facts, Notice, Section } from './sections'

const STATUS = {
  observed: { label: 'Data observed', icon: Eye, className: 'text-success' },
  no_data: { label: 'No data in window', icon: CircleSlash, className: 'text-warning' },
  unknown: { label: 'Coverage unknown', icon: CircleHelp, className: 'text-muted-foreground' },
} as const

function Names({ values }: { values: string[] }) {
  if (values.length === 0) return <span className="text-muted-foreground">none</span>
  return (
    <span className="flex flex-wrap gap-1">
      {values.map((v) => (
        <code key={v} className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
          {v}
        </code>
      ))}
    </span>
  )
}

function ScopeSection({ environment }: { environment: EnvironmentDetail }) {
  const { scope } = environment
  return (
    <Section
      title="Telemetry scope"
      badge={
        <Badge variant={scope.status === 'configured' ? 'secondary' : 'outline'}>
          {humanize(scope.status)}
        </Badge>
      }
      description="The SigNoz identities this environment's investigations may query. Every query is restricted to them (v1 isolation on a shared SigNoz; it trusts telemetry identity attributes)."
    >
      <div className="space-y-4">
        <Facts
          items={[
            ['Hosts (host metrics, system logs)', <Names key="h" values={scope.hosts} />],
            ['Services (traces, application logs)', <Names key="s" values={scope.services} />],
            ['Deployment environment', scope.deployment_environment ?? '—'],
          ]}
        />
        {scope.problems.length > 0 && (
          <ul className="space-y-2" aria-label="Scope problems">
            {scope.problems.map((p, i) => (
              <li
                key={i}
                className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-sm"
              >
                <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0 text-warning" />
                <span>
                  {p.message}{' '}
                  <code className="font-mono text-xs text-muted-foreground">{p.code}</code>
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Section>
  )
}

function CoverageTable({ coverage }: { coverage: TelemetryCoverage }) {
  return (
    <div className="-mx-6 overflow-x-auto px-6">
      <table className="w-full min-w-max text-sm">
        <caption className="sr-only">Telemetry availability as last recorded</caption>
        <thead>
          <tr className="border-b text-left text-xs text-muted-foreground">
            <th scope="col" className="py-2 pr-4 font-medium">
              Signal
            </th>
            <th scope="col" className="py-2 pr-4 font-medium">
              Identity
            </th>
            <th scope="col" className="py-2 pr-4 font-medium">
              Last result
            </th>
            <th scope="col" className="py-2 pr-4 font-medium">
              Window queried (UTC)
            </th>
            <th scope="col" className="py-2 font-medium">
              Recorded (your time)
            </th>
          </tr>
        </thead>
        <tbody>
          {coverage.signals.map((s) => {
            const status = STATUS[s.status]
            return (
              <tr key={`${s.signal}:${s.identity}:${s.value}`} className="border-b last:border-b-0">
                <th scope="row" className="py-2 pr-4 text-left font-medium capitalize">
                  {s.signal}
                </th>
                <td className="py-2 pr-4">
                  <span className="text-xs text-muted-foreground">{s.identity}</span>{' '}
                  <code className="font-mono text-xs">{s.value}</code>
                </td>
                <td className="py-2 pr-4">
                  <span className={`inline-flex items-center gap-1.5 ${status.className}`}>
                    <status.icon aria-hidden className="size-3.5" />
                    {status.label}
                  </span>
                </td>
                <td className="py-2 pr-4 text-xs text-muted-foreground">
                  {s.window_start && s.window_end
                    ? formatWindow({ start: s.window_start, end: s.window_end })
                    : '—'}
                </td>
                <td className="py-2 text-xs">
                  <Link
                    to={`/investigations/${s.investigation_id}`}
                    className="text-primary underline-offset-2 hover:underline"
                  >
                    {formatDateTime(s.checked_at)}
                  </Link>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function CoverageSection({ environmentId }: { environmentId: string }) {
  const coverage = useTelemetryCoverage(environmentId)
  return (
    <Section
      title="Recorded telemetry availability"
      description="What the latest query of each signal found, as recorded by this environment's investigations. Historical observations with their times, not a live health check: no query is run to fill this in."
    >
      {coverage.isPending ? (
        <SectionSkeleton label="Loading telemetry availability" />
      ) : coverage.error ? (
        <ErrorState error={coverage.error} onRetry={() => coverage.refetch()} />
      ) : coverage.data.signals.length === 0 ? (
        <EmptyState
          icon={Activity}
          title="Nothing recorded yet"
          description="Availability appears here after an investigation has queried this environment's telemetry."
        />
      ) : (
        <div className="space-y-3">
          <CoverageTable coverage={coverage.data} />
          <p className="text-xs text-muted-foreground">
            Based on {coverage.data.queries_checked} successful queries
            {coverage.data.queries_failed > 0 &&
              `; ${coverage.data.queries_failed} queries failed or timed out and are not counted`}
            .
          </p>
        </div>
      )}
    </Section>
  )
}

export function TelemetryTab({ environment }: { environment: EnvironmentDetail }) {
  const provider = environment.telemetry_provider
  return (
    <div className="space-y-4">
      <ScopeSection environment={environment} />
      <CoverageSection environmentId={environment.id} />
      <Section
        title="SigNoz connection"
        badge={<ConfiguredBadge configured={provider.configured} />}
        description="Telemetry storage and queries (metrics, logs, traces)."
      >
        <div className="space-y-3">
          <Facts
            items={[
              ['Source', PROVIDER_SOURCE[provider.source]],
              ['Credentials', 'Never shown or sent to the browser'],
            ]}
          />
          {provider.source === 'environment' ? (
            <Notice tone="warning">
              Configuration required. This environment has no telemetry connection of its own, and
              the backend&apos;s development connection is never shared with it; its investigations
              cannot query telemetry yet.
            </Notice>
          ) : (
            provider.issues.length > 0 && (
              <Notice tone="warning">
                Missing or invalid settings: {provider.issues.join(', ')}. Telemetry queries fail
                until they are set in the backend&apos;s .env.
              </Notice>
            )
          )}
          <Notice>
            &ldquo;Configured&rdquo; means the settings are present and well-formed, not that SigNoz
            was reachable. Reachability shows up in the recorded availability above.
          </Notice>
        </div>
      </Section>
      <CredentialForm kind="signoz" />
    </div>
  )
}
