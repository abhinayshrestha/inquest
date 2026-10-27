import { PageHeader } from '@/components/page-header'

import { CreateEnvironment } from './create-environment'
import { EnvironmentList } from './environment-list'

export default function EnvironmentsPage() {
  return (
    <>
      <PageHeader
        title="Environments"
        description="Infrastructure Inquest investigates: telemetry scope, provider status and investigations."
      />
      <div className="space-y-6">
        <EnvironmentList />
        <CreateEnvironment />
      </div>
    </>
  )
}
