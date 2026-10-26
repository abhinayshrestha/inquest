import { KeyRound, UserRound } from 'lucide-react'

import { useTheme, type Theme } from '@/app/contexts/theme'
import { THEME_OPTIONS } from '@/app/layout/theme-options'
import { EmptyState } from '@/components/empty-state'
import { PageHeader } from '@/components/page-header'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from 'cn'

function AppearanceCard() {
  const { theme, setTheme } = useTheme()
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Appearance</CardTitle>
        <CardDescription>Stored in this browser only.</CardDescription>
      </CardHeader>
      <CardContent>
        <fieldset>
          <legend className="sr-only">Theme</legend>
          <div className="grid grid-cols-3 gap-2">
            {THEME_OPTIONS.map(({ value, label, icon: Icon }) => (
              <label
                key={value}
                className={cn(
                  'flex cursor-pointer flex-col items-center gap-2 rounded-md border p-3 text-sm has-focus-visible:ring-2 has-focus-visible:ring-ring',
                  theme === value && 'border-primary bg-accent',
                )}
              >
                <input
                  type="radio"
                  name="theme"
                  value={value}
                  checked={theme === value}
                  onChange={() => setTheme(value as Theme)}
                  className="sr-only"
                />
                <Icon aria-hidden className="size-4" />
                {label}
              </label>
            ))}
          </div>
        </fieldset>
      </CardContent>
    </Card>
  )
}

export default function SettingsPage() {
  return (
    <>
      <PageHeader title="Settings" description="Personal preferences and organization settings." />
      <div className="grid gap-4 lg:grid-cols-2">
        <AppearanceCard />
        <EmptyState
          icon={UserRound}
          badge="Planned"
          title="Profile and organization settings"
          description="Available once authentication and organizations exist."
        />
        <EmptyState
          className="lg:col-span-2"
          icon={KeyRound}
          badge="Planned"
          title="Credentials are configured per environment"
          description="Nebius and telemetry credentials will be set by each environment's owner on the environment, never here, and never shown after saving. Today the backend reads them from its own .env file; the browser never receives them."
        />
      </div>
    </>
  )
}
