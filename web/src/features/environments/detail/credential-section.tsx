import type { ReactNode } from 'react'

import { ErrorState } from '@/components/error-state'
import { SectionSkeleton } from '@/components/skeletons'
import type { EnvironmentDetail } from '@/lib/api'
import { ISOLATION_LABEL, KIND_LABEL, type CredentialKind } from '@/lib/api/credentials'
import { formatDateTime } from '@/lib/format'

import { useCredentials } from '../use-credentials'
import { CredentialEditor, KindBadge } from './credential-form'
import { Facts, Notice, Section } from './sections'
import { useOwnership } from './use-ownership'

function Model({ id }: { id: string | null | undefined }) {
  return id ? <code className="font-mono text-xs break-all">{id}</code> : '—'
}

// Why runs of this environment can or cannot use its credentials, from the same resolver
// runs use (environment detail `credentials` and provider issues). Shared by both kinds:
// the resolver decides for the environment as a whole.
export function Readiness({ environment }: { environment: EnvironmentDetail }) {
  const issues = [...environment.ai_provider.issues, ...environment.telemetry_provider.issues]
  switch (environment.credentials) {
    case 'environment':
      return (
        <Notice>
          Ready: investigations of this environment run on its own Nebius and SigNoz credentials.
        </Notice>
      )
    case 'process_development':
      return (
        <Notice>
          This environment runs on the backend’s development settings, granted by an operator for
          local development. Credentials saved here replace that grant for this environment.
        </Notice>
      )
    case 'unavailable':
      return (
        <Notice tone="warning">
          {issues.includes('destination_not_allowed')
            ? 'Stored endpoint not allowed: a saved provider URL is outside this server’s current destination policy. The owner has to replace it.'
            : issues.includes('telemetry_isolation_unverified')
              ? 'Telemetry not verified as isolated from other organizations, so investigations cannot use it. An operator has to review this connection; the owner can also switch to another SigNoz.'
              : 'Stored but unreadable: this server cannot decrypt the saved credentials (its encryption keys changed). The owner has to replace them, or an operator restores the keys.'}{' '}
          Investigations cannot run until then.
        </Notice>
      )
    default:
      return (
        <Notice tone="warning">
          Configuration required: investigations need both a Nebius and a SigNoz credential of this
          environment’s own. The backend’s development settings are never shared with it.
        </Notice>
      )
  }
}

export function CredentialSection({
  environment,
  kind,
  description,
  facts = [],
  children,
}: {
  environment: EnvironmentDetail
  kind: CredentialKind
  description?: ReactNode
  facts?: [string, ReactNode][]
  children?: ReactNode
}) {
  const credentials = useCredentials(environment.id)
  const { isOwner, ownerName, nameOf } = useOwnership(environment)
  const status = credentials.data?.find((s) => s.kind === kind)
  return (
    <Section
      title={`${KIND_LABEL[kind]} credentials`}
      badge={<KindBadge status={status} />}
      description={description}
    >
      {credentials.isPending ? (
        <SectionSkeleton label={`Loading ${KIND_LABEL[kind]} credential status`} />
      ) : credentials.error ? (
        <ErrorState error={credentials.error} onRetry={() => credentials.refetch()} />
      ) : (
        <div className="space-y-4">
          <Facts
            items={[
              ...(kind === 'nebius'
                ? ([
                    [
                      'Main Investigator model',
                      <Model
                        key="m"
                        id={status?.main_model ?? environment.ai_provider.main_model}
                      />,
                    ],
                    [
                      'Local Investigator model',
                      <Model
                        key="l"
                        id={status?.local_model ?? environment.ai_provider.local_model}
                      />,
                    ],
                  ] as [string, ReactNode][])
                : []),
              ...facts,
              ...(kind === 'signoz' && status?.isolation
                ? ([['Isolation', ISOLATION_LABEL[status.isolation]]] as [string, ReactNode][])
                : []),
              [
                'Status',
                status?.state === 'configured'
                  ? 'Configured'
                  : status?.state === 'unavailable'
                    ? 'Unavailable: saved with an encryption key this server does not have'
                    : 'Missing',
              ],
              [
                'Last set',
                status?.updated_at
                  ? `${formatDateTime(status.updated_at)} by ${nameOf(status.updated_by)}`
                  : '—',
              ],
              ['Owner', ownerName],
            ]}
          />
          {children}
          <CredentialEditor
            environmentId={environment.id}
            kind={kind}
            status={status}
            isOwner={isOwner}
            ownerName={ownerName}
          />
        </div>
      )}
    </Section>
  )
}
