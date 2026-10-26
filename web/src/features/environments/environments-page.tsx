import { PageHeader } from '@/components/page-header'

import { EnvironmentList } from './environment-list'

export default function EnvironmentsPage() {
  return (
    <>
      <PageHeader
        title="Environments"
        description="Infrastructure Inquest investigates. Configuration and credentials will be managed here by each environment's owner (planned)."
      />
      <EnvironmentList />
    </>
  )
}
