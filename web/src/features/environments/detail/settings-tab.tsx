import { ArrowRightLeft, Pencil, Power, UserRound } from 'lucide-react'

import { useAuth } from '@/app/contexts/auth'
import { Button } from '@/components/ui/button'
import type { EnvironmentDetail } from '@/lib/api'
import { PLANNED_TRANSFER_STEPS } from '@/lib/api/organizations'
import { formatDateTime } from '@/lib/format'

import { Facts, Notice, PlannedBadge, Section } from './sections'

export function SettingsTab({ environment }: { environment: EnvironmentDetail }) {
  const { user } = useAuth()
  return (
    <div className="space-y-4">
      <Section title="Details">
        <Facts
          items={[
            ['Name', environment.name],
            ['Type', environment.type === 'aws' ? 'AWS' : 'Local'],
            ['Status', environment.status],
            ['Created', formatDateTime(environment.created_at)],
            ['Last updated', formatDateTime(environment.updated_at)],
            [
              'ID',
              <code key="id" className="font-mono text-xs break-all">
                {environment.id}
              </code>,
            ],
          ]}
        />
      </Section>

      <Section
        title="Ownership"
        badge={<PlannedBadge>Transfer planned</PlannedBadge>}
        description="The user who creates an environment owns it. Only the owner configures its credentials; organization administrators manage members but not environment credentials."
      >
        <div className="space-y-4">
          <p className="flex items-center gap-2 text-sm">
            <UserRound aria-hidden className="size-4 text-muted-foreground" />
            Owner:{' '}
            <span className="text-muted-foreground">
              {environment.owner_user_id === null
                ? 'none (created before organizations)'
                : environment.owner_user_id === user?.id
                  ? 'you'
                  : 'another member of this organization'}
            </span>
          </p>
          <div>
            <h3 className="mb-2 text-sm font-medium">Planned ownership transfer</h3>
            <ol className="list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
              {PLANNED_TRANSFER_STEPS.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
          </div>
          <Button variant="outline" disabled>
            <ArrowRightLeft aria-hidden />
            Transfer ownership
          </Button>
        </div>
      </Section>

      <Section
        title="Manage environment"
        badge={<PlannedBadge>Owner only</PlannedBadge>}
        description="Renaming, scope changes and disabling will be available to the environment owner."
      >
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" disabled>
              <Pencil aria-hidden />
              Rename
            </Button>
            <Button variant="outline" disabled>
              <Power aria-hidden />
              Disable environment
            </Button>
          </div>
          <Notice>
            Environments are never deleted: investigations and evidence keep referencing them, so
            retiring one means disabling it (history is retained, D006).
          </Notice>
        </div>
      </Section>
    </div>
  )
}
