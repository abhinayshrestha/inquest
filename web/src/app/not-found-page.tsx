import { Compass } from 'lucide-react'
import { Link } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { PageHeader } from '@/components/page-header'
import { Button } from '@/components/ui/button'

export function NotFoundPage() {
  return (
    <>
      <PageHeader title="Page not found" />
      <EmptyState
        icon={Compass}
        title="This page does not exist"
        description="Check the address, or go back to the overview."
        action={
          <Button asChild size="sm">
            <Link to="/">Go to overview</Link>
          </Button>
        }
      />
    </>
  )
}
