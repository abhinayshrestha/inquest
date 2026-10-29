import { Building2, Check, ChevronRight, Loader2, Minus, Plus } from 'lucide-react'
import { useId, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router'

import { useAuth } from '@/app/contexts/auth'
import { useOrganization } from '@/app/contexts/organization'
import { EmptyState } from '@/components/empty-state'
import { PageHeader } from '@/components/page-header'
import { ListSkeleton } from '@/components/skeletons'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PERMISSIONS } from '@/lib/api/organizations'
import { describeError } from '@/lib/errors'
import { humanize } from '@/lib/format'

import { useCreateOrganization } from './queries'
import { SignInRequired } from './sign-in-required'

function Allowed({ allowed }: { allowed: boolean }) {
  return allowed ? (
    <Check aria-label="Allowed" className="mx-auto size-4 text-success" />
  ) : (
    <Minus aria-label="Not allowed" className="mx-auto size-4 text-muted-foreground" />
  )
}

function PermissionMatrix() {
  return (
    <div className="-mx-6 overflow-x-auto px-6">
      <table className="w-full min-w-max text-sm">
        <caption className="sr-only">Permissions by role</caption>
        <thead>
          <tr className="border-b text-xs text-muted-foreground">
            <th scope="col" className="py-2 pr-4 text-left font-medium">
              Action
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Org owner
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Org admin
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Member
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Environment owner
            </th>
            <th scope="col" className="px-3 py-2 text-left font-medium">
              Status
            </th>
          </tr>
        </thead>
        <tbody>
          {PERMISSIONS.map((p) => (
            <tr key={p.action} className="border-b last:border-b-0">
              <th scope="row" className="py-2 pr-4 text-left font-normal">
                {p.action}
              </th>
              <td className="px-3 py-2">
                <Allowed allowed={p.owner} />
              </td>
              <td className="px-3 py-2">
                <Allowed allowed={p.admin} />
              </td>
              <td className="px-3 py-2">
                <Allowed allowed={p.member} />
              </td>
              <td className="px-3 py-2">
                <Allowed allowed={p.environmentOwner} />
              </td>
              <td className="px-3 py-2 text-xs">
                {p.enforced ? (
                  <span className="text-success">Enforced</span>
                ) : (
                  <span className="text-muted-foreground">Planned</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function CreateOrganization() {
  const create = useCreateOrganization()
  const navigate = useNavigate()
  const { selectOrganization } = useOrganization()
  const [name, setName] = useState('')
  const inputId = useId()

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!name.trim()) return
    const organization = await create.mutateAsync(name.trim()).catch(() => null)
    if (organization) {
      selectOrganization(organization.id)
      navigate(`/organizations/${organization.id}`)
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-2">
      <Label htmlFor={inputId}>New organization</Label>
      <div className="flex gap-2">
        <Input
          id={inputId}
          value={name}
          maxLength={120}
          onChange={(e) => setName(e.target.value)}
          placeholder="Acme Platform Team"
        />
        <Button type="submit" disabled={create.isPending || !name.trim()}>
          {create.isPending ? (
            <Loader2 aria-hidden className="animate-spin" />
          ) : (
            <Plus aria-hidden />
          )}
          Create
        </Button>
      </div>
      {create.error && (
        <p role="alert" className="text-sm text-destructive">
          {describeError(create.error).description}
        </p>
      )}
      <p className="text-xs text-muted-foreground">
        You become its owner. You can invite teammates afterwards.
      </p>
    </form>
  )
}

export default function OrganizationsPage() {
  const { status } = useAuth()
  const { organizations, loading } = useOrganization()
  return (
    <>
      <PageHeader
        title="Organizations"
        description="Teams you belong to, their members and invitations."
      />
      <div className="space-y-4">
        {status !== 'signed_in' ? (
          <SignInRequired
            returnTo="/organizations"
            what="create organizations and invite teammates"
          />
        ) : (
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Your organizations</CardTitle>
            </CardHeader>
            <CardContent className="space-y-5">
              {loading ? (
                <ListSkeleton rows={2} />
              ) : organizations.length === 0 ? (
                <EmptyState
                  icon={Building2}
                  title="No organizations yet"
                  description="Create one, or accept an invitation from a teammate."
                />
              ) : (
                <ul className="divide-y rounded-lg border" aria-label="Your organizations">
                  {organizations.map((o) => (
                    <li key={o.organization_id}>
                      <Link
                        to={`/organizations/${o.organization_id}`}
                        className="flex items-center gap-3 p-3 hover:bg-accent/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset"
                      >
                        <Building2 aria-hidden className="size-4 text-muted-foreground" />
                        <span className="min-w-0 flex-1 truncate font-medium">{o.name}</span>
                        <Badge variant="outline">{humanize(o.role)}</Badge>
                        <ChevronRight aria-hidden className="size-4 text-muted-foreground" />
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
              <CreateOrganization />
            </CardContent>
          </Card>
        )}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Roles and permissions</CardTitle>
            <CardDescription>
              Organization administration and environment ownership are separate: owners and admins
              manage people, while only an environment&apos;s owner will set its credentials.
              Environments, investigations and reports belong to one organization and only its
              members can see them.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <PermissionMatrix />
          </CardContent>
        </Card>
      </div>
    </>
  )
}
