import { useState } from 'react'
import { createBrowserRouter, RouterProvider } from 'react-router'

import { ErrorBoundary } from '@/components/error-boundary'

import { AppProviders } from './providers'
import { routes } from './routes'

export function App() {
  const [router] = useState(() => createBrowserRouter(routes))
  return (
    // Last resort for failures outside any route (e.g. a provider throwing).
    <ErrorBoundary>
      <AppProviders>
        <RouterProvider router={router} />
      </AppProviders>
    </ErrorBoundary>
  )
}
