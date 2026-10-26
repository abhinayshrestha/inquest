import { Building2, ChevronsUpDown, Info } from 'lucide-react'
import { Link } from 'react-router'

import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'

import { useOrganization } from '../contexts/organization'

// Placeholder only: shows that no organization exists rather than a fake one (D025).
export function OrganizationSwitcher() {
  const { selectedOrganization } = useOrganization()
  const label = selectedOrganization?.name ?? 'No organization'
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="max-w-56 justify-between gap-2">
          <Building2 aria-hidden />
          <span className="truncate">{label}</span>
          <ChevronsUpDown aria-hidden className="opacity-60" />
          <span className="sr-only">Switch organization</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-72">
        <DropdownMenuLabel>Organizations</DropdownMenuLabel>
        <div className="flex gap-2 px-2 py-1.5 text-xs text-muted-foreground">
          <Info aria-hidden className="mt-0.5 size-3.5 shrink-0" />
          <p>
            Organizations and teammate invitations are planned. The backend has no organizations or
            authentication yet, so all local data is shared.
          </p>
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link to="/organizations">About organizations</Link>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
