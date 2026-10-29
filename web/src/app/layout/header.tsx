import { ShieldAlert } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { IncomingTransfers } from '@/features/environments/incoming-transfers'

import { MobileNav } from './mobile-nav'
import { OrganizationSwitcher } from './organization-switcher'
import { ThemeMenu } from './theme-menu'
import { UserMenu } from './user-menu'

export function Header() {
  return (
    <header className="sticky top-0 z-20 flex h-14 items-center gap-2 border-b bg-background/95 px-4 backdrop-blur supports-backdrop-filter:bg-background/80 sm:px-6">
      <MobileNav />
      <OrganizationSwitcher />
      <div className="ml-auto flex items-center gap-2">
        <Tooltip>
          <TooltipTrigger asChild>
            <Badge variant="outline" tabIndex={0} className="hidden gap-1 sm:inline-flex">
              <ShieldAlert aria-hidden className="text-warning" />
              Private preview
            </Badge>
          </TooltipTrigger>
          <TooltipContent className="max-w-72">
            Data is isolated per organization and environments run on their own credentials, but the
            remaining isolation gaps keep this private. Not for public deployment.
          </TooltipContent>
        </Tooltip>
        <IncomingTransfers />
        <ThemeMenu />
        <UserMenu />
      </div>
    </header>
  )
}
