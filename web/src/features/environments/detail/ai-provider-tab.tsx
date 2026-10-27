import type { EnvironmentDetail } from '@/lib/api'

import { CredentialForm } from './credential-form'
import { PROVIDER_SOURCE } from './provider-source'
import { ConfiguredBadge, Facts, Notice, Section } from './sections'

export function AiProviderTab({ environment }: { environment: EnvironmentDetail }) {
  const ai = environment.ai_provider
  return (
    <div className="space-y-4">
      <Section
        title="Nebius Token Factory"
        badge={<ConfiguredBadge configured={ai.configured} />}
        description="NVIDIA Nemotron models that reason for the Main and Local Investigators. Models never receive credentials."
      >
        <div className="space-y-3">
          <Facts
            items={[
              [
                'Main Investigator model',
                ai.main_model ? <code className="font-mono text-xs">{ai.main_model}</code> : '—',
              ],
              [
                'Local Investigator model',
                ai.local_model ? <code className="font-mono text-xs">{ai.local_model}</code> : '—',
              ],
              ['Source', PROVIDER_SOURCE[ai.source]],
              ['API key', 'Never shown or sent to the browser'],
            ]}
          />
          {ai.source === 'environment' ? (
            <Notice tone="warning">
              Configuration required. This environment has no AI provider credentials of its own,
              and the backend&apos;s development credentials are never shared with it, so it cannot
              run investigations yet. Per-environment credentials, set by the environment&apos;s
              owner, arrive in a later phase.
            </Notice>
          ) : (
            ai.issues.length > 0 && (
              <Notice tone="warning">
                Missing or invalid settings: {ai.issues.join(', ')}. Investigations cannot run until
                they are set in the backend&apos;s .env.
              </Notice>
            )
          )}
          <Notice>
            &ldquo;Configured&rdquo; means the settings are present and well-formed; this page never
            calls the model provider to check them.
          </Notice>
        </div>
      </Section>
      <CredentialForm kind="nebius" />
    </div>
  )
}
