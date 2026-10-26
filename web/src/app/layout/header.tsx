import { ShieldAlert } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

import { MobileNav } from './mobile-nav'
import { OrganizationSwitcher } from './organization-switcher'
import { ThemeMenu } from './theme-menu'

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
              Local development
            </Badge>
          </TooltipTrigger>
          <TooltipContent className="max-w-64">
            No authentication or tenant isolation yet. Run Inquest locally only.
          </TooltipContent>
        </Tooltip>
        <ThemeMenu />
      </div>
    </header>
  )
}
