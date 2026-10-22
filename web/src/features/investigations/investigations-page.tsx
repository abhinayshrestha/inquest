import { Search } from 'lucide-react'

import { EmptyState } from '@/components/empty-state'
import { PageHeader } from '@/components/page-header'

import { OpenInvestigationForm } from './open-investigation-form'

export default function InvestigationsPage() {
  return (
    <>
      <PageHeader
        title="Investigations"
        description="Conversational investigations: questions, evidence and evolving reports."
      />
      <div className="space-y-4">
        <EmptyState
          icon={Search}
          title="Investigation list not available yet"
          description="The backend cannot list investigations yet. Creating and starting investigations from the UI comes with the investigation experience."
          requires={['GET /investigations']}
        />
        <EmptyState
          icon={Search}
          badge="Development only"
          title="Open an investigation by ID"
          description="Open an existing investigation, for example one created with scripts/run_investigation.py."
          action={<OpenInvestigationForm />}
        />
      </div>
    </>
  )
}
