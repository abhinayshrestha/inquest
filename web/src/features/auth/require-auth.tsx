import { Loader2 } from 'lucide-react'
import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router'

import { useAuth } from '@/app/contexts/auth'
import { LogoMark } from '@/components/logo'

import { loginPath } from './login-path'

export function FullPageLoader({ label }: { label: string }) {
  return (
    <div role="status" className="flex min-h-dvh flex-col items-center justify-center gap-4">
      <LogoMark className="size-11" />
      <span className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 aria-hidden className="size-4 animate-spin" />
        {label}
      </span>
    </div>
  )
}

// Every page of the app needs a signed-in session; the server checks it on every request
// anyway (D030, D033), this only sends visitors to the login page instead of empty pages.
export function RequireAuth({ children }: { children: ReactNode }) {
  const { status, ended } = useAuth()
  const location = useLocation()
  if (status === 'loading') return <FullPageLoader label="Checking your session" />
  if (status !== 'signed_in') return <Navigate to={loginPath(location, ended)} replace />
  return children
}
