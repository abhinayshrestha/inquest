import { FileText } from 'lucide-react'
import { Link } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { PageHeader } from '@/components/page-header'
import { Button } from '@/components/ui/button'

export default function ReportsPage() {
  return (
    <>
      <PageHeader
        title="Reports"
        description="Evidence-backed investigation reports and their version history."
      />
      <EmptyState
        icon={FileText}
        title="Report list not available yet"
        description="Reports are versioned per investigation and can be read from an investigation. A list across investigations needs a backend endpoint."
        requires={['GET /reports (or an equivalent list)']}
        action={
          <Button asChild variant="outline" size="sm">
            <Link to="/investigations">Go to investigations</Link>
          </Button>
        }
      />
    </>
  )
}
