import { useEffect, useRef } from 'react'
import { Outlet, useLocation } from 'react-router'

import { AuthErrorBanner } from './auth-error-banner'
import { Header } from './header'
import { Sidebar } from './sidebar'

export function AppShell() {
  const { pathname } = useLocation()
  const mainRef = useRef<HTMLElement>(null)
  const firstRender = useRef(true)

  // Move focus to the new page on client-side navigation so keyboard and screen reader
  // users start at the content, as they would after a full page load.
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false
      return
    }
    mainRef.current?.focus()
  }, [pathname])

  return (
    <div className="flex min-h-dvh">
      <a
        href="#main-content"
        className="sr-only z-50 rounded-md bg-primary px-3 py-2 text-primary-foreground focus:not-sr-only focus:fixed focus:top-2 focus:left-2"
      >
        Skip to content
      </a>
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <Header />
        <main
          id="main-content"
          ref={mainRef}
          tabIndex={-1}
          className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 outline-none sm:px-6 lg:px-8"
        >
          <AuthErrorBanner />
          <Outlet />
        </main>
      </div>
    </div>
  )
}
