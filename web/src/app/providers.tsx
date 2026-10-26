import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'

import { TooltipProvider } from '@/components/ui/tooltip'
import { createQueryClient } from '@/lib/query-client'

import { OrganizationProvider } from './contexts/organization'
import { ThemeProvider } from './contexts/theme'
import { UiProvider } from './contexts/ui'

export function AppProviders({
  children,
  queryClient,
}: {
  children: ReactNode
  queryClient?: QueryClient
}) {
  const [client] = useState(() => queryClient ?? createQueryClient())
  return (
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <OrganizationProvider>
          <UiProvider>
            <TooltipProvider delayDuration={300}>{children}</TooltipProvider>
          </UiProvider>
        </OrganizationProvider>
      </ThemeProvider>
    </QueryClientProvider>
  )
}
