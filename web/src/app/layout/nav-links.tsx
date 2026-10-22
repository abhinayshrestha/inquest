import { NavLink } from 'react-router'

import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from 'cn'

import { NAV_ITEMS } from './nav'

export function NavLinks({
  collapsed = false,
  onNavigate,
}: {
  collapsed?: boolean
  onNavigate?: () => void
}) {
  return (
    <ul className="flex flex-col gap-1">
      {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => {
        const link = (
          <NavLink
            to={to}
            end={end}
            onClick={onNavigate}
            aria-label={collapsed ? label : undefined}
            className={({ isActive }) =>
              cn(
                'flex h-9 items-center gap-3 rounded-md px-3 text-sm font-medium transition-colors',
                'hover:bg-sidebar-accent hover:text-sidebar-accent-foreground',
                'outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring',
                collapsed && 'justify-center px-0',
                isActive
                  ? 'bg-sidebar-accent text-sidebar-accent-foreground'
                  : 'text-sidebar-foreground/80',
              )
            }
          >
            <Icon aria-hidden className="size-4 shrink-0" />
            {!collapsed && <span>{label}</span>}
          </NavLink>
        )
        return (
          <li key={to}>
            {collapsed ? (
              <Tooltip>
                <TooltipTrigger asChild>{link}</TooltipTrigger>
                <TooltipContent side="right">{label}</TooltipContent>
              </Tooltip>
            ) : (
              link
            )}
          </li>
        )
      })}
    </ul>
  )
}
