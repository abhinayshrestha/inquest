import { KeyRound } from 'lucide-react'
import { useId } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

import { Notice, PlannedBadge, Section } from './sections'

type Field = { name: string; label: string; secret?: boolean; placeholder?: string; hint?: string }

const FIELDS: Record<'nebius' | 'signoz', Field[]> = {
  nebius: [
    {
      name: 'api_key',
      label: 'API key',
      secret: true,
      hint: 'Write-only: never shown again after saving.',
    },
    {
      name: 'base_url',
      label: 'Base URL',
      placeholder: 'https://api.tokenfactory.nebius.com/v1/',
      hint: 'https only.',
    },
    {
      name: 'main_model',
      label: 'Main Investigator model',
      placeholder: 'nvidia/Nemotron-3-Ultra-550b-a55b',
    },
    {
      name: 'local_model',
      label: 'Local Investigator model',
      placeholder: 'nvidia/nemotron-3-super-120b-a12b',
    },
  ],
  signoz: [
    { name: 'base_url', label: 'SigNoz URL', placeholder: 'https://signoz.example.internal' },
    {
      name: 'api_key',
      label: 'API key (Viewer role)',
      secret: true,
      hint: 'Write-only: never shown again after saving.',
    },
  ],
}

// The planned per-environment credential form (D026). Rendered disabled: there is no
// endpoint to send it to, because without authentication anyone could set or replace an
// environment's credentials, and there is no encrypted storage yet. Inputs are disabled
// so nothing can be typed, kept in state or sent. Validation rules: lib/api/credentials.ts.
export function CredentialForm({ kind }: { kind: 'nebius' | 'signoz' }) {
  const base = useId()
  const title =
    kind === 'nebius' ? 'Per-environment Nebius credentials' : 'Per-environment SigNoz connection'
  return (
    <Section
      title={title}
      badge={<PlannedBadge>Owner only · planned</PlannedBadge>}
      description="Only this environment's owner will be able to set or replace these. Saved values are encrypted on the server and never returned; the page only shows whether they are configured."
    >
      <form aria-label={title} onSubmit={(e) => e.preventDefault()} className="space-y-4">
        <fieldset disabled className="grid gap-4 sm:grid-cols-2">
          <legend className="sr-only">{title} (disabled)</legend>
          {FIELDS[kind].map((f) => {
            const id = `${base}-${f.name}`
            return (
              <div key={f.name} className="space-y-1.5">
                <Label htmlFor={id}>{f.label}</Label>
                <Input
                  id={id}
                  name={f.name}
                  type={f.secret ? 'password' : 'text'}
                  autoComplete="off"
                  placeholder={f.placeholder}
                  aria-describedby={f.hint ? `${id}-hint` : undefined}
                />
                {f.hint && (
                  <p id={`${id}-hint`} className="text-xs text-muted-foreground">
                    {f.hint}
                  </p>
                )}
              </div>
            )
          })}
        </fieldset>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" disabled>
            <KeyRound aria-hidden />
            Save credentials
          </Button>
          <span className="text-xs text-muted-foreground">
            Disabled until encrypted per-environment credentials exist.
          </span>
        </div>
        <Notice>
          The backend&apos;s own development configuration (
          {kind === 'nebius' ? 'NEBIUS_*' : 'SIGNOZ_*'} settings in its .env) serves only
          environments an operator explicitly granted it. Never paste credentials into the browser.
        </Notice>
      </form>
    </Section>
  )
}
