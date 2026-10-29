import { ArrowRightLeft } from 'lucide-react'
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
import { formatDateTime } from '@/lib/format'

import { useTransfers } from './use-transfers'

// Ownership offers waiting for the signed-in user, in the header so they are found from
// any page. Each opens the environment's settings, where it is accepted or declined (an
// environment of another organization asks to switch first).
export function IncomingTransfers() {
  const transfers = useTransfers()
  const incoming = transfers.data?.filter((t) => t.direction === 'incoming') ?? []
  if (incoming.length === 0) return null
  const label = `${incoming.length} ownership ${incoming.length === 1 ? 'offer' : 'offers'} waiting`
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" aria-label={label}>
          <ArrowRightLeft aria-hidden className="text-primary" />
          <span aria-hidden>{incoming.length}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-72">
        <DropdownMenuLabel>{label}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {incoming.map((t) => (
          <DropdownMenuItem key={t.id} asChild>
            <Link
              to={`/environments/${t.environment_id}?tab=settings`}
              className="flex-col items-start"
            >
              <span className="truncate font-medium">{t.environment_name}</span>
              <span className="text-xs text-muted-foreground">
                Offered {formatDateTime(t.created_at)}
              </span>
            </Link>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
