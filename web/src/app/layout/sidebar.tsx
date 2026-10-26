import { PanelLeftClose, PanelLeftOpen } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { cn } from 'cn'

import { useUi } from '../contexts/ui'
import { Brand } from './brand'
import { NavLinks } from './nav-links'

// Desktop only (lg and up); below that the same links live in MobileNav.
export function Sidebar() {
  const { sidebarCollapsed, toggleSidebar } = useUi()
  return (
    <aside
      className={cn(
        'sticky top-0 hidden h-dvh shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground lg:flex',
        sidebarCollapsed ? 'w-16' : 'w-60',
      )}
    >
      <div className={cn('flex h-14 items-center px-4', sidebarCollapsed && 'justify-center px-2')}>
        <Brand compact={sidebarCollapsed} />
      </div>
      <nav aria-label="Main" className="flex-1 overflow-y-auto px-3 py-2">
        <NavLinks collapsed={sidebarCollapsed} />
      </nav>
      <div
        className={cn(
          'border-t border-sidebar-border p-3',
          sidebarCollapsed && 'flex justify-center',
        )}
      >
        <Button
          variant="ghost"
          size={sidebarCollapsed ? 'icon' : 'sm'}
          onClick={toggleSidebar}
          aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          aria-expanded={!sidebarCollapsed}
          className={cn(!sidebarCollapsed && 'w-full justify-start')}
        >
          {sidebarCollapsed ? <PanelLeftOpen aria-hidden /> : <PanelLeftClose aria-hidden />}
          {!sidebarCollapsed && 'Collapse'}
        </Button>
      </div>
    </aside>
  )
}
