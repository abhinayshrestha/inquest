import { useId } from 'react'

import { cn } from 'cn'

// The Inquest mark: a lens over a telemetry trace, the moment a signal is found. A brand
// asset, so its colours are fixed (the same as public/favicon.svg) rather than themed.
export function LogoMark({ className }: { className?: string }) {
  const gradient = useId()
  return (
    <svg
      viewBox="0 0 32 32"
      aria-hidden
      className={cn('size-8 shrink-0 drop-shadow-md drop-shadow-primary/30', className)}
    >
      <defs>
        <linearGradient id={gradient} x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#4338ca" />
          <stop offset="1" stopColor="#0f9488" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="9" fill={`url(#${gradient})`} />
      <g fill="none" strokeLinecap="round" strokeLinejoin="round" stroke="#fff">
        <circle cx="14" cy="14" r="7.25" strokeWidth="2.25" />
        <path d="m19.6 19.6 5.4 5.4" strokeWidth="2.75" />
        <path d="M9.5 15h2.2l1.6-4.2 2.4 6.6 1.5-3.4h1.3" strokeWidth="1.6" />
      </g>
      <circle cx="19.1" cy="14" r="1.15" fill="#fff" />
    </svg>
  )
}

// "in" set light and "quest" bold: the question asked, then the investigation that answers it.
export function Wordmark({ className }: { className?: string }) {
  return (
    <span className={cn('text-[1.35rem] leading-none tracking-[-0.045em] lowercase', className)}>
      <span className="font-light text-muted-foreground">in</span>
      <span className="font-bold">quest</span>
    </span>
  )
}

export function Logo({ className, markClassName }: { className?: string; markClassName?: string }) {
  return (
    <span className={cn('flex items-center gap-2.5', className)}>
      <LogoMark className={markClassName} />
      <Wordmark />
    </span>
  )
}
