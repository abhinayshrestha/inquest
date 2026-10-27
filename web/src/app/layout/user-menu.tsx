import { Loader2, LogIn, LogOut, UserRound } from 'lucide-react'

import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'

import { useAuth } from '../contexts/auth'

export function UserMenu() {
  const { status, user, signIn, signOut, signingOut } = useAuth()
  if (status === 'loading')
    return (
      <Loader2
        aria-label="Checking sign-in"
        className="size-4 animate-spin text-muted-foreground"
      />
    )
  if (status === 'unconfigured' || status === 'unavailable') return null
  if (status === 'signed_out' || !user) {
    return (
      <Button size="sm" onClick={() => signIn()}>
        <LogIn aria-hidden />
        Sign in
      </Button>
    )
  }
  const name = user.display_name || user.email || 'Signed in'
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="sm" aria-label={`Account: ${name}`} className="max-w-48">
          <UserRound aria-hidden />
          <span className="hidden truncate sm:inline">{name}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel className="space-y-0.5">
          <p className="truncate">{name}</p>
          {user.email && (
            <p className="truncate text-xs font-normal text-muted-foreground">{user.email}</p>
          )}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => void signOut()} disabled={signingOut}>
          <LogOut aria-hidden />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
