import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, Plus } from 'lucide-react'
import { useId, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router'

import { useOrganization } from '@/app/contexts/organization'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { createEnvironment, environmentCreate, type EnvironmentCreate } from '@/lib/api'
import { describeError } from '@/lib/errors'

const SELECT =
  'h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm dark:bg-input/30'

function names(value: string) {
  return [
    ...new Set(
      value
        .split(',')
        .map((v) => v.trim())
        .filter(Boolean),
    ),
  ].slice(0, 20)
}

// Creates an environment in the selected organization; the server checks membership and
// records the creator as owner. Only names and telemetry scope (identity names) are sent:
// there is no credential field, and the environment starts configuration-required.
export function CreateEnvironment() {
  const { selectedOrganization } = useOrganization()
  const client = useQueryClient()
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [type, setType] = useState<EnvironmentCreate['type']>('local')
  const [hosts, setHosts] = useState('')
  const [services, setServices] = useState('')
  const [invalid, setInvalid] = useState<string | null>(null)
  const ids = { name: useId(), type: useId(), hosts: useId(), services: useId() }
  const create = useMutation({
    mutationFn: createEnvironment,
    onSuccess: (environment) => {
      void client.invalidateQueries({ queryKey: ['environments'] })
      navigate(`/environments/${environment.id}`)
    },
  })
  if (!selectedOrganization) return null

  function submit(event: FormEvent) {
    event.preventDefault()
    const scope = { hosts: names(hosts), services: names(services) }
    const parsed = environmentCreate.safeParse({
      organization_id: selectedOrganization!.organization_id,
      name: name.trim(),
      type,
      metadata_json: scope.hosts.length || scope.services.length ? { signoz: scope } : {},
    })
    if (!parsed.success) {
      setInvalid('Enter a name of up to 255 characters.')
      return
    }
    setInvalid(null)
    create.mutate(parsed.data)
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">New environment</CardTitle>
        <CardDescription>
          Created in {selectedOrganization.name} with you as its owner. It needs its own credentials
          before it can run investigations; those arrive in a later phase.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2" noValidate>
          <div className="space-y-1.5">
            <Label htmlFor={ids.name}>Name</Label>
            <Input
              id={ids.name}
              value={name}
              maxLength={255}
              onChange={(e) => setName(e.target.value)}
              placeholder="checkout-prod"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={ids.type}>Type</Label>
            <select
              id={ids.type}
              className={SELECT}
              value={type}
              onChange={(e) => setType(e.target.value as EnvironmentCreate['type'])}
            >
              <option value="local">Local</option>
              <option value="aws">AWS</option>
            </select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={ids.hosts}>Hosts (comma separated)</Label>
            <Input id={ids.hosts} value={hosts} onChange={(e) => setHosts(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={ids.services}>Services (comma separated)</Label>
            <Input
              id={ids.services}
              value={services}
              onChange={(e) => setServices(e.target.value)}
            />
          </div>
          <div className="flex flex-wrap items-center gap-3 sm:col-span-2">
            <Button type="submit" disabled={create.isPending || !name.trim()}>
              {create.isPending ? (
                <Loader2 aria-hidden className="animate-spin" />
              ) : (
                <Plus aria-hidden />
              )}
              Create environment
            </Button>
            {(invalid || create.error) && (
              <p role="alert" className="text-sm text-destructive">
                {invalid ?? describeError(create.error).description}
              </p>
            )}
          </div>
        </form>
      </CardContent>
    </Card>
  )
}
