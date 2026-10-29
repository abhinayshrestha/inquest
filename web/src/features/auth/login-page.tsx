import { useQueryClient } from '@tanstack/react-query'
import {
  ArrowRight,
  CircleCheck,
  FileSearch,
  Lightbulb,
  Loader2,
  LockKeyhole,
  MessageSquareText,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Target,
  type LucideIcon,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { Navigate, useSearchParams } from 'react-router'

import { SESSION_KEY, useAuth } from '@/app/contexts/auth'
import { Logo } from '@/components/logo'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'

import { authErrorMessage } from './auth-errors'
import { safeReturnTo } from './login-path'
import { FullPageLoader } from './require-auth'

const SLOGANS = [
  'Root causes, not dashboards.',
  'From alert to answer, with the evidence attached.',
  'Every claim cites its evidence.',
  'Inconclusive beats confidently wrong.',
]
const SLOGAN_MS = 4500

// How an investigation runs, as a description of the product: no sample data or results.
const STEPS: { icon: LucideIcon; title: string; text: string }[] = [
  { icon: MessageSquareText, title: 'Ask', text: 'Describe the failure in plain language.' },
  { icon: Lightbulb, title: 'Hypothesize', text: 'Agents propose what could explain it.' },
  { icon: FileSearch, title: 'Gather evidence', text: 'Targeted, read-only telemetry queries.' },
  { icon: Target, title: 'Conclude', text: 'A root-cause report you can verify.' },
]

const PILLARS: { icon: LucideIcon; text: string }[] = [
  { icon: CircleCheck, text: 'Evidence-backed answers' },
  { icon: ShieldCheck, text: 'Read-only by design' },
  { icon: Sparkles, text: 'NVIDIA Nemotron on Nebius' },
]

function useRotatingSlogan() {
  const [index, setIndex] = useState(0)
  useEffect(() => {
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return
    const timer = window.setInterval(() => setIndex((i) => (i + 1) % SLOGANS.length), SLOGAN_MS)
    return () => window.clearInterval(timer)
  }, [])
  return SLOGANS[index]!
}

// Always dark, whatever the theme: the `dark` class switches the tokens for this subtree.
function Showcase() {
  const slogan = useRotatingSlogan()
  return (
    <aside className="dark relative hidden overflow-hidden bg-background text-foreground lg:flex lg:flex-col lg:justify-between lg:p-12 xl:p-16">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 bg-[linear-gradient(to_right,var(--border)_1px,transparent_1px),linear-gradient(to_bottom,var(--border)_1px,transparent_1px)] [mask-image:radial-gradient(ellipse_70%_60%_at_40%_40%,black,transparent)] bg-[size:44px_44px]"
      />
      <div
        aria-hidden
        className="pointer-events-none absolute -top-40 -left-32 size-[34rem] rounded-full bg-primary/25 blur-3xl"
      />
      <div
        aria-hidden
        className="pointer-events-none absolute -right-40 -bottom-48 size-[30rem] rounded-full bg-chart-2/15 blur-3xl"
      />

      <Logo className="relative" />

      <div className="relative max-w-xl space-y-8">
        <div className="space-y-5">
          <p className="inline-flex items-center gap-2 rounded-full border bg-card/60 px-3 py-1 text-xs font-medium text-muted-foreground backdrop-blur">
            <span className="size-1.5 rounded-full bg-success" aria-hidden />
            Autonomous infrastructure investigation
          </p>
          <h2 className="text-4xl leading-tight font-semibold tracking-tight text-balance xl:text-5xl">
            Know <span className="text-primary">why</span> production broke, not just that it did.
          </h2>
          <p
            key={slogan}
            aria-live="off"
            className="h-7 text-lg text-muted-foreground motion-safe:animate-in motion-safe:duration-500 motion-safe:fade-in motion-safe:slide-in-from-bottom-1"
          >
            {slogan}
          </p>
        </div>

        <ol className="grid gap-3 rounded-2xl border bg-card/50 p-5 shadow-2xl shadow-black/40 backdrop-blur">
          {STEPS.map(({ icon: Icon, title, text }, i) => (
            <li key={title} className="flex items-center gap-4">
              <span className="relative flex size-9 shrink-0 items-center justify-center rounded-lg border bg-background/70 text-primary">
                <Icon aria-hidden className="size-4" />
              </span>
              <div className="min-w-0">
                <p className="text-sm font-medium">
                  <span className="mr-2 font-mono text-xs text-muted-foreground">0{i + 1}</span>
                  {title}
                </p>
                <p className="text-sm text-muted-foreground">{text}</p>
              </div>
            </li>
          ))}
        </ol>
      </div>

      <ul className="relative flex flex-wrap gap-x-6 gap-y-2 text-sm text-muted-foreground">
        {PILLARS.map(({ icon: Icon, text }) => (
          <li key={text} className="flex items-center gap-2">
            <Icon aria-hidden className="size-4 text-primary" />
            {text}
          </li>
        ))}
      </ul>
    </aside>
  )
}

function Notice() {
  const [params] = useSearchParams()
  const error = params.get('auth_error')
  const signedOut = params.get('signed_out')
  if (error) {
    return (
      <Alert variant="destructive">
        <ShieldAlert aria-hidden />
        <AlertTitle>Sign-in failed</AlertTitle>
        <AlertDescription>{authErrorMessage(error)}</AlertDescription>
      </Alert>
    )
  }
  if (signedOut === 'signed_out' || signedOut === 'everywhere') {
    return (
      <Alert role="status">
        <CircleCheck aria-hidden className="text-success!" />
        <AlertTitle>
          {signedOut === 'everywhere' ? 'Signed out everywhere' : 'You have signed out'}
        </AlertTitle>
        <AlertDescription>
          {signedOut === 'everywhere'
            ? 'Every Inquest session of this account has ended, on every device.'
            : 'Your session has ended. Sign in again to continue.'}
        </AlertDescription>
      </Alert>
    )
  }
  return null
}

function SignInPanel({ returnTo }: { returnTo: string }) {
  const { status, signIn } = useAuth()
  const client = useQueryClient()
  const [redirecting, setRedirecting] = useState(false)

  if (status === 'unconfigured') {
    return (
      <Alert>
        <ShieldAlert aria-hidden className="text-warning!" />
        <AlertTitle>Sign-in is not configured</AlertTitle>
        <AlertDescription>
          This server has no identity provider (INQUEST_AUTH_* settings), so nobody can sign in.
          There is deliberately no fallback identity.
        </AlertDescription>
      </Alert>
    )
  }
  if (status === 'unavailable') {
    return (
      <div className="space-y-4">
        <Alert variant="destructive">
          <ShieldAlert aria-hidden />
          <AlertTitle>Inquest is unreachable</AlertTitle>
          <AlertDescription>The server did not answer. Check that it is running.</AlertDescription>
        </Alert>
        <Button
          variant="outline"
          className="w-full"
          onClick={() => void client.invalidateQueries({ queryKey: SESSION_KEY })}
        >
          <RefreshCw aria-hidden />
          Try again
        </Button>
      </div>
    )
  }
  return (
    <div className="space-y-3">
      <Button
        size="lg"
        className="group h-11 w-full text-[15px] shadow-lg shadow-primary/25"
        disabled={redirecting}
        onClick={() => {
          setRedirecting(true)
          signIn(returnTo)
        }}
      >
        {redirecting ? (
          <Loader2 aria-hidden className="animate-spin" />
        ) : (
          <LockKeyhole aria-hidden />
        )}
        {redirecting ? 'Redirecting…' : 'Continue with single sign-on'}
        {!redirecting && (
          <ArrowRight
            aria-hidden
            className="ml-auto transition-transform group-hover:translate-x-0.5"
          />
        )}
      </Button>
      <p className="text-center text-xs text-muted-foreground">
        You will be redirected to your identity provider. Inquest never sees your password.
      </p>
    </div>
  )
}

export default function LoginPage() {
  const { status } = useAuth()
  const [params] = useSearchParams()
  const returnTo = safeReturnTo(params.get('return_to'))

  if (status === 'loading') return <FullPageLoader label="Checking your session" />
  if (status === 'signed_in') return <Navigate to={returnTo} replace />

  return (
    <div className="grid min-h-dvh bg-background lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
      <Showcase />
      <main className="relative flex flex-col px-6 py-6 sm:px-10">
        <Logo className="lg:hidden" />

        <div className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center gap-8 py-12">
          <div className="space-y-2">
            <p className="text-sm font-medium text-primary lg:hidden">{SLOGANS[0]}</p>
            <h1 className="text-3xl font-semibold tracking-tight">Welcome back</h1>
            <p className="text-muted-foreground">
              Sign in to investigate incidents with your team.
            </p>
          </div>

          <Notice />
          <SignInPanel returnTo={returnTo} />

          <Separator />

          <div className="space-y-3 text-sm text-muted-foreground">
            <p className="flex gap-2.5">
              <ShieldCheck aria-hidden className="mt-0.5 size-4 shrink-0 text-primary" />
              Access is by invitation or operator approval. Your organization's data stays isolated
              from every other organization.
            </p>
            <p className="flex gap-2.5">
              <MessageSquareText aria-hidden className="mt-0.5 size-4 shrink-0 text-primary" />
              Invited by a teammate? Open the invitation link you were sent.
            </p>
          </div>
        </div>

        <footer className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
          <span></span>
          <span>© {new Date().getFullYear()} Inquest</span>
        </footer>
      </main>
    </div>
  )
}
