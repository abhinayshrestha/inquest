import {
  Building2,
  FileText,
  LayoutDashboard,
  Search,
  Server,
  Settings,
  type LucideIcon,
} from 'lucide-react'

export type NavItem = { to: string; label: string; icon: LucideIcon; end?: boolean }

export const NAV_ITEMS: readonly NavItem[] = [
  { to: '/', label: 'Overview', icon: LayoutDashboard, end: true },
  { to: '/investigations', label: 'Investigations', icon: Search },
  { to: '/environments', label: 'Environments', icon: Server },
  { to: '/reports', label: 'Reports', icon: FileText },
  { to: '/organizations', label: 'Organizations', icon: Building2 },
  { to: '/settings', label: 'Settings', icon: Settings },
]
