import { Pencil, Power } from 'lucide-react'

import { Button } from '@/components/ui/button'
import type { EnvironmentDetail } from '@/lib/api'
import { formatDateTime } from '@/lib/format'

import { OwnershipSection } from './ownership-section'
import { Facts, Notice, PlannedBadge, Section } from './sections'

export function SettingsTab({ environment }: { environment: EnvironmentDetail }) {
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

      <OwnershipSection environment={environment} />

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
