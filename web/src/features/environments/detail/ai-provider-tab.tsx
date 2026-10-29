import type { EnvironmentDetail } from '@/lib/api'

import { CredentialSection, Readiness } from './credential-section'
import { PROVIDER_SOURCE } from './provider-source'
import { ConfiguredBadge, Facts, Notice, Section } from './sections'

export function AiProviderTab({ environment }: { environment: EnvironmentDetail }) {
  const ai = environment.ai_provider
  const development = environment.credentials === 'process_development'
  return (
    <div className="space-y-4">
      <CredentialSection
        environment={environment}
        kind="nebius"
        description="The Nebius Token Factory key and NVIDIA Nemotron models the Main and Local Investigators reason with. The key is encrypted on the server and never shown again; models never receive it."
        facts={[['API key and URL', 'Never shown or sent to the browser']]}
      >
        <Readiness environment={environment} />
      </CredentialSection>
      {development && (
        <Section
          title="Development settings"
          badge={<ConfiguredBadge configured={ai.configured} />}
          description={PROVIDER_SOURCE.process_environment}
        >
          <div className="space-y-3">
            <Facts items={[['Source', PROVIDER_SOURCE[ai.source]]]} />
            {ai.issues.length > 0 && (
              <Notice tone="warning">
                Missing or invalid settings: {ai.issues.join(', ')}. Investigations cannot run until
                they are set in the backend&apos;s .env.
              </Notice>
            )}
          </div>
        </Section>
      )}
      <Notice>
        Status comes from what is stored and whether this server can use it; this page never calls
        the model provider to check it. Saving new credentials does check them once.
      </Notice>
    </div>
  )
}
