import {
  ArrowLeft,
  Building2,
  Check,
  Copy,
  Loader2,
  MailPlus,
  Trash2,
  UserMinus,
} from 'lucide-react'
import { useId, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router'

import { useAuth } from '@/app/contexts/auth'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { PageHeader } from '@/components/page-header'
import { PageSkeleton, ListSkeleton } from '@/components/skeletons'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  inviteInput,
  type InvitationCreated,
  type Member,
  type Organization,
  type OrganizationRole,
} from '@/lib/api/organizations'
import { ApiError } from '@/lib/api'
import { describeError } from '@/lib/errors'
import { formatDateTime, humanize } from '@/lib/format'
import { isUuid } from '@/lib/ids'

import {
  useChangeRole,
  useCreateInvitation,
  useInvitations,
  useMembers,
  useOrganizationDetail,
  useRemoveMember,
  useRevokeInvitation,
} from './queries'
import { SignInRequired } from './sign-in-required'

const SELECT =
  'h-8 rounded-md border border-input bg-transparent px-2 text-sm disabled:opacity-50 dark:bg-input/30'

function BackLink() {
  return (
    <Button asChild variant="ghost" size="sm" className="mb-2 -ml-2">
      <Link to="/organizations">
        <ArrowLeft aria-hidden />
        Organizations
      </Link>
    </Button>
  )
}

// The UI only offers what the role allows; the server enforces the same rules anyway.
function MemberRow({
  member,
  organization,
  selfId,
}: {
  member: Member
  organization: Organization
  selfId: string
}) {
  const changeRole = useChangeRole(organization.id)
  const remove = useRemoveMember(organization.id)
  const navigate = useNavigate()
  const self = member.user_id === selfId
  const canRemove =
    self ||
    organization.role === 'owner' ||
    (organization.role === 'admin' && member.role === 'member')
  const error = changeRole.error ?? remove.error
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-2 p-3">
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium">
          {member.display_name || member.email || 'Unnamed user'}
          {self && <span className="text-muted-foreground"> (you)</span>}
        </p>
        <p className="truncate text-xs text-muted-foreground">
          {member.email ?? 'no email'} · joined {formatDateTime(member.joined_at)}
        </p>
        {error && (
          <p role="alert" className="text-xs text-destructive">
            {describeError(error).description}
          </p>
        )}
      </div>
      {organization.role === 'owner' ? (
        <select
          aria-label={`Role of ${member.display_name || member.email}`}
          className={SELECT}
          value={member.role}
          disabled={changeRole.isPending}
          onChange={(e) =>
            changeRole.mutate({ userId: member.user_id, role: e.target.value as OrganizationRole })
          }
        >
          {(['owner', 'admin', 'member'] as const).map((r) => (
            <option key={r} value={r}>
              {humanize(r)}
            </option>
          ))}
        </select>
      ) : (
        <Badge variant={member.role === 'owner' ? 'secondary' : 'outline'}>
          {humanize(member.role)}
        </Badge>
      )}
      {canRemove && (
        <Button
          variant="ghost"
          size="sm"
          disabled={remove.isPending}
          onClick={() =>
            remove.mutate(member.user_id, { onSuccess: () => self && navigate('/organizations') })
          }
          aria-label={self ? 'Leave organization' : `Remove ${member.display_name || member.email}`}
        >
          <UserMinus aria-hidden />
          <span className="hidden sm:inline">{self ? 'Leave' : 'Remove'}</span>
        </Button>
      )}
    </li>
  )
}

function CreatedLink({ created, onDone }: { created: InvitationCreated; onDone: () => void }) {
  const [copied, setCopied] = useState(false)
  return (
    <div
      role="status"
      className="space-y-2 rounded-md border border-success/30 bg-success/10 p-3 text-sm"
    >
      <p className="font-medium">Invitation created for {created.email}</p>
      <p className="text-xs text-muted-foreground">
        Send this link to them yourself. It is shown only now, works once, only for {created.email}{' '}
        signed in with a verified email, and expires {formatDateTime(created.expires_at)}.
      </p>
      <div className="flex gap-2">
        <Input
          readOnly
          value={created.accept_url}
          aria-label="Invitation link"
          className="font-mono text-xs"
          onFocus={(e) => e.currentTarget.select()}
        />
        <Button
          type="button"
          variant="outline"
          onClick={() => {
            void navigator.clipboard?.writeText(created.accept_url).then(() => setCopied(true))
          }}
        >
          {copied ? <Check aria-hidden /> : <Copy aria-hidden />}
          {copied ? 'Copied' : 'Copy'}
        </Button>
      </div>
      <Button type="button" variant="ghost" size="sm" onClick={onDone}>
        Done
      </Button>
    </div>
  )
}

function Invitations({ organization }: { organization: Organization }) {
  const list = useInvitations(organization.id)
  const create = useCreateInvitation(organization.id)
  const revoke = useRevokeInvitation(organization.id)
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<'member' | 'admin'>('member')
  const [invalid, setInvalid] = useState<string | null>(null)
  // Held only in component state until "Done": never stored, never refetched.
  const [created, setCreated] = useState<InvitationCreated | null>(null)
  const ids = { email: useId(), role: useId() }

  async function submit(event: FormEvent) {
    event.preventDefault()
    const parsed = inviteInput.safeParse({ email: email.trim(), role })
    if (!parsed.success) {
      setInvalid(parsed.error.issues[0]?.message ?? 'Check the email address')
      return
    }
    setInvalid(null)
    const result = await create.mutateAsync(parsed.data).catch(() => null)
    if (result) {
      setCreated(result)
      setEmail('')
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Invitations</CardTitle>
        <CardDescription>
          Invite teammates by email. Existing and new users join by opening the link while signed in
          with that address.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {created ? (
          <CreatedLink created={created} onDone={() => setCreated(null)} />
        ) : (
          <form
            onSubmit={(e) => void submit(e)}
            className="grid gap-3 sm:grid-cols-[1fr_9rem_auto] sm:items-end"
            noValidate
          >
            <div className="space-y-1.5">
              <Label htmlFor={ids.email}>Email</Label>
              <Input
                id={ids.email}
                type="email"
                autoComplete="off"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="teammate@example.com"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={ids.role}>Role</Label>
              <select
                id={ids.role}
                className={`${SELECT} h-9 w-full`}
                value={role}
                onChange={(e) => setRole(e.target.value as 'member' | 'admin')}
              >
                <option value="member">Member</option>
                {organization.role === 'owner' && <option value="admin">Admin</option>}
              </select>
            </div>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? (
                <Loader2 aria-hidden className="animate-spin" />
              ) : (
                <MailPlus aria-hidden />
              )}
              Invite
            </Button>
            {(invalid || create.error) && (
              <p role="alert" className="text-sm text-destructive sm:col-span-3">
                {invalid ?? describeError(create.error).description}
              </p>
            )}
          </form>
        )}
        {list.isPending ? (
          <ListSkeleton rows={2} />
        ) : list.error ? (
          <ErrorState error={list.error} onRetry={() => list.refetch()} />
        ) : list.data.length === 0 ? (
          <p className="text-sm text-muted-foreground">No invitations yet.</p>
        ) : (
          <ul className="divide-y rounded-lg border" aria-label="Invitations">
            {list.data.map((i) => (
              <li key={i.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 p-3 text-sm">
                <span className="min-w-0 flex-1 truncate">{i.email}</span>
                <Badge variant="outline">{humanize(i.role)}</Badge>
                <span className="text-xs text-muted-foreground">
                  {humanize(i.status)}
                  {i.status === 'pending' && ` · expires ${formatDateTime(i.expires_at)}`}
                </span>
                {i.status === 'pending' && (
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={revoke.isPending}
                    onClick={() => revoke.mutate(i.id)}
                    aria-label={`Revoke invitation for ${i.email}`}
                  >
                    <Trash2 aria-hidden />
                    <span className="hidden sm:inline">Revoke</span>
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

function OrganizationView({ orgId }: { orgId: string }) {
  const { user } = useAuth()
  const detail = useOrganizationDetail(orgId)
  const members = useMembers(orgId)
  if (detail.isPending) return <PageSkeleton variant="detail" />
  if (detail.error) {
    if (detail.error instanceof ApiError && detail.error.kind === 'not_found') {
      return (
        <>
          <BackLink />
          <EmptyState
            icon={Building2}
            title="Organization not found"
            description="It does not exist, or you are not a member of it."
          />
        </>
      )
    }
    return (
      <>
        <BackLink />
        <ErrorState error={detail.error} onRetry={() => detail.refetch()} />
      </>
    )
  }
  const organization = detail.data
  const manager = organization.role === 'owner' || organization.role === 'admin'
  return (
    <>
      <BackLink />
      <PageHeader
        title={organization.name}
        description={
          <span className="inline-flex flex-wrap items-center gap-2">
            <code className="font-mono text-xs">{organization.slug}</code>
            <Badge variant="secondary">
              You are{' '}
              {organization.role === 'admin'
                ? 'an admin'
                : `a${organization.role === 'owner' ? 'n owner' : ' member'}`}
            </Badge>
            <span>
              {organization.member_count} member{organization.member_count === 1 ? '' : 's'}
            </span>
          </span>
        }
      />
      <div className="space-y-4">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Members</CardTitle>
          </CardHeader>
          <CardContent>
            {members.isPending ? (
              <ListSkeleton rows={2} />
            ) : members.error ? (
              <ErrorState error={members.error} onRetry={() => members.refetch()} />
            ) : (
              <ul className="divide-y rounded-lg border" aria-label="Members">
                {members.data.map((m) => (
                  <MemberRow
                    key={m.user_id}
                    member={m}
                    organization={organization}
                    selfId={user?.id ?? ''}
                  />
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
        {manager && <Invitations organization={organization} />}
        <p className="text-xs text-muted-foreground">
          Every member can see this organization&apos;s environments, investigations and reports.
          Removing a member ends their access at once, including open live views.
        </p>
      </div>
    </>
  )
}

export default function OrganizationDetailPage() {
  const { orgId } = useParams()
  const { status } = useAuth()
  if (status !== 'signed_in') {
    return (
      <>
        <BackLink />
        <SignInRequired returnTo={`/organizations/${orgId ?? ''}`} what="see this organization" />
      </>
    )
  }
  if (!isUuid(orgId)) {
    return (
      <>
        <BackLink />
        <EmptyState
          icon={Building2}
          title="Organization not found"
          description="It does not exist, or you are not a member of it."
        />
      </>
    )
  }
  return <OrganizationView key={orgId} orgId={orgId} />
}
