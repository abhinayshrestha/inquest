import { ArrowRightLeft, Check, Loader2, UserRound, X } from 'lucide-react'
import { useId, useState } from 'react'
import { Link } from 'react-router'

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import type { EnvironmentDetail } from '@/lib/api'
import { describeError } from '@/lib/errors'
import { formatDateTime } from '@/lib/format'
import type { Transfer } from '@/lib/api/transfers'

import { useProposeTransfer, useResolveTransfer, useTransfers } from '../use-transfers'
import { Notice, Section } from './sections'
import { useOwnership } from './use-ownership'

// Environment ownership (D029, D035). The owner offers it to another member; only that
// member accepts or declines; the owner can withdraw the offer. Accepting deletes the
// previous owner's credentials on the server. The server checks every step; these
// controls only appear for the people it will allow.

const CONSEQUENCES =
  'The previous owner’s Nebius and SigNoz credentials for this environment are deleted, never handed over, and its running investigations are cancelled. Past investigations, evidence and reports stay with the environment. It cannot run investigations until the new owner saves their own credentials.'

function Failure({ error }: { error: unknown }) {
  if (!error) return null
  const { title, description } = describeError(error)
  return (
    <p
      role="alert"
      className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm"
    >
      {title}: {description}
    </p>
  )
}

function Confirm({
  open,
  onOpenChange,
  title,
  description,
  action,
  onConfirm,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: string
  action: string
  onConfirm: () => void
}) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{description}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Go back</AlertDialogCancel>
          <AlertDialogAction onClick={onConfirm}>{action}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

function Propose({
  environment,
  candidates,
}: {
  environment: EnvironmentDetail
  candidates: { id: string; name: string }[]
}) {
  const id = useId()
  const [recipient, setRecipient] = useState('')
  const [confirming, setConfirming] = useState(false)
  const propose = useProposeTransfer(environment.id)
  const name = candidates.find((c) => c.id === recipient)?.name ?? ''
  if (candidates.length === 0)
    return (
      <Notice>
        No other members to transfer it to. Invite someone to this organization first.
      </Notice>
    )
  return (
    <div className="space-y-3">
      <form
        aria-label="Transfer ownership"
        className="flex flex-col gap-2 sm:flex-row sm:items-end"
        onSubmit={(e) => {
          e.preventDefault()
          if (recipient) setConfirming(true)
        }}
      >
        <div className="min-w-0 flex-1 space-y-1.5">
          <Label htmlFor={id}>New owner</Label>
          <select
            id={id}
            value={recipient}
            onChange={(e) => setRecipient(e.target.value)}
            className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm shadow-xs focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none dark:bg-input/30"
          >
            <option value="">Choose a member…</option>
            {candidates.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>
        <Button type="submit" variant="outline" disabled={!recipient || propose.isPending}>
          {propose.isPending ? (
            <Loader2 aria-hidden className="animate-spin" />
          ) : (
            <ArrowRightLeft aria-hidden />
          )}
          Offer ownership
        </Button>
      </form>
      <Failure error={propose.error} />
      <Confirm
        open={confirming}
        onOpenChange={setConfirming}
        title={`Offer this environment to ${name}?`}
        description={`Nothing changes until ${name} accepts; you can withdraw the offer until then. If they accept, they become the owner. ${CONSEQUENCES}`}
        action="Send offer"
        onConfirm={() => propose.mutate(recipient, { onSuccess: () => setRecipient('') })}
      />
    </div>
  )
}

function Outgoing({
  environment,
  transfer,
  to,
}: {
  environment: EnvironmentDetail
  transfer: Transfer
  to: string
}) {
  const resolve = useResolveTransfer(environment.id)
  return (
    <div className="space-y-3 rounded-md border px-4 py-3">
      <p className="flex flex-wrap items-center gap-2 text-sm">
        <Badge variant="outline">Pending</Badge>
        Offered to <span className="font-medium">{to}</span> on{' '}
        {formatDateTime(transfer.created_at)}. Nothing changes until they accept.
      </p>
      <Button
        variant="outline"
        size="sm"
        disabled={resolve.isPending}
        onClick={() => resolve.mutate({ id: transfer.id, action: 'cancel' })}
      >
        <X aria-hidden />
        Withdraw offer
      </Button>
      <Failure error={resolve.error} />
    </div>
  )
}

function Incoming({
  environment,
  transfer,
  from,
}: {
  environment: EnvironmentDetail
  transfer: Transfer
  from: string
}) {
  const [confirming, setConfirming] = useState(false)
  const resolve = useResolveTransfer(environment.id)
  return (
    <div
      className="space-y-3 rounded-md border border-primary/40 bg-primary/5 px-4 py-3"
      role="region"
      aria-label="Ownership offer"
    >
      <p className="text-sm">
        <span className="font-medium">{from}</span> offered you ownership of this environment on{' '}
        {formatDateTime(transfer.created_at)}.
      </p>
      <p className="text-xs text-muted-foreground">{CONSEQUENCES}</p>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" disabled={resolve.isPending} onClick={() => setConfirming(true)}>
          {resolve.isPending ? (
            <Loader2 aria-hidden className="animate-spin" />
          ) : (
            <Check aria-hidden />
          )}
          Accept ownership
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={resolve.isPending}
          onClick={() => resolve.mutate({ id: transfer.id, action: 'decline' })}
        >
          <X aria-hidden />
          Decline
        </Button>
      </div>
      <Failure error={resolve.error} />
      <Confirm
        open={confirming}
        onOpenChange={setConfirming}
        title="Become the owner of this environment?"
        description={CONSEQUENCES}
        action="Accept ownership"
        onConfirm={() => resolve.mutate({ id: transfer.id, action: 'accept' })}
      />
    </div>
  )
}

export function OwnershipSection({ environment }: { environment: EnvironmentDetail }) {
  const { isOwner, ownerName, nameOf, members } = useOwnership(environment)
  const transfers = useTransfers()
  const pending = transfers.data?.filter((t) => t.environment_id === environment.id) ?? []
  const outgoing = pending.find((t) => t.direction === 'outgoing')
  const incoming = pending.find((t) => t.direction === 'incoming')
  const candidates = (members.data ?? [])
    .filter((m) => m.user_id !== environment.owner_user_id)
    .map((m) => ({ id: m.user_id, name: m.display_name || m.email || 'Member' }))
  return (
    <Section
      title="Ownership"
      description="The owner configures this environment's credentials and can hand it to another member. Organization owners and admins manage people, not environment credentials."
    >
      <div className="space-y-4">
        <p className="flex items-center gap-2 text-sm">
          <UserRound aria-hidden className="size-4 text-muted-foreground" />
          Owner: <span className="font-medium">{ownerName}</span>
        </p>
        {isOwner && environment.credentials === 'configuration_required' && (
          <Notice tone="warning">
            You own this environment but it has no credentials yet.{' '}
            <Link to="?tab=ai" className="underline underline-offset-2">
              Set up Nebius
            </Link>{' '}
            and{' '}
            <Link to="?tab=telemetry" className="underline underline-offset-2">
              SigNoz
            </Link>{' '}
            to run investigations.
          </Notice>
        )}
        {incoming && (
          <Incoming
            environment={environment}
            transfer={incoming}
            from={nameOf(incoming.from_user_id)}
          />
        )}
        {isOwner &&
          (outgoing ? (
            <Outgoing
              environment={environment}
              transfer={outgoing}
              to={nameOf(outgoing.to_user_id)}
            />
          ) : (
            <Propose environment={environment} candidates={candidates} />
          ))}
        {!isOwner && !incoming && (
          <p className="text-sm text-muted-foreground">
            Only the owner can transfer this environment.
          </p>
        )}
      </div>
    </Section>
  )
}
