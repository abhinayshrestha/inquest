import { Building2, Check, ChevronsUpDown, Plus } from 'lucide-react'
import { Link, useLocation, useNavigate } from 'react-router'

import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { humanize } from '@/lib/format'

import { useAuth } from '../contexts/auth'
import { useOrganization } from '../contexts/organization'

// Switches which organization the UI shows. Switching is a preference; every
// organization page is still authorized by the server for the signed-in user.
export function OrganizationSwitcher() {
  const { status } = useAuth()
  const { organizations, selectedOrganization, selectOrganization } = useOrganization()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const label =
    status !== 'signed_in' ? 'Organizations' : (selectedOrganization?.name ?? 'No organization')

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
        {status !== 'signed_in' ? (
          <p className="px-2 py-1.5 text-xs text-muted-foreground">
            {status === 'unconfigured'
              ? 'Sign-in is not configured on this server.'
              : 'Sign in to see your organizations.'}
          </p>
        ) : organizations.length === 0 ? (
          <p className="px-2 py-1.5 text-xs text-muted-foreground">
            You are not a member of any organization yet.
          </p>
        ) : (
          organizations.map((o) => (
            <DropdownMenuItem
              key={o.organization_id}
              onSelect={() => {
                selectOrganization(o.organization_id)
                // Data pages stay put and reload for the new organization; on the
                // organization pages, open the one just selected.
                if (pathname.startsWith('/organizations')) {
                  navigate(`/organizations/${o.organization_id}`)
                }
              }}
            >
              <Check
                aria-hidden
                className={
                  o.organization_id === selectedOrganization?.organization_id ? '' : 'invisible'
                }
              />
              <span className="min-w-0 flex-1 truncate">{o.name}</span>
              <span className="text-xs text-muted-foreground">{humanize(o.role)}</span>
            </DropdownMenuItem>
          ))
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link to="/organizations">
            <Plus aria-hidden />
            {status === 'signed_in' ? 'Create or manage organizations' : 'About organizations'}
          </Link>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
