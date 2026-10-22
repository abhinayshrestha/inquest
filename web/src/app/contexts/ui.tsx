import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'

import { readPreference, writePreference } from '@/lib/storage'

type UiState = {
  sidebarCollapsed: boolean
  toggleSidebar: () => void
  mobileNavOpen: boolean
  setMobileNavOpen: (open: boolean) => void
}

const UiContext = createContext<UiState | null>(null)

export function UiProvider({ children }: { children: ReactNode }) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => readPreference('sidebarCollapsed') === 'true',
  )
  const [mobileNavOpen, setMobileNavOpen] = useState(false)

  const value = useMemo<UiState>(
    () => ({
      sidebarCollapsed,
      toggleSidebar: () =>
        setSidebarCollapsed((collapsed) => {
          writePreference('sidebarCollapsed', String(!collapsed))
          return !collapsed
        }),
      mobileNavOpen,
      setMobileNavOpen,
    }),
    [sidebarCollapsed, mobileNavOpen],
  )

  return <UiContext value={value}>{children}</UiContext>
}

export function useUi() {
  const context = useContext(UiContext)
  if (!context) throw new Error('useUi must be used inside UiProvider')
  return context
}
