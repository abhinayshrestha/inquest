import { AlertTriangle, CheckCircle2, KeyRound, Loader2, Trash2 } from 'lucide-react'
import { useId, useState, type FormEvent } from 'react'

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
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  ISOLATION_LABEL,
  KIND_LABEL,
  NEBIUS_DEFAULT_URL,
  nebiusCredentialsInput,
  signozCredentialsInput,
  type CredentialKind,
  type CredentialStatus,
  type TelemetryIsolation,
} from '@/lib/api/credentials'

import {
  credentialError,
  lostAccess,
  useRemoveCredential,
  useSaveCredential,
} from '../use-credentials'
import { Notice } from './sections'

// Per-environment credentials (D034), set only by the environment's owner. Values live in
// this form's state while it is open and nowhere else: never prefilled from the server
// (which never returns them), never cached or stored, cleared on success, cancel and
// unmount. The server re-checks ownership on every write; hiding the form from others is
// only a convenience.

export function KindBadge({ status }: { status: CredentialStatus | undefined }) {
  if (!status) return null
  if (status.state === 'configured')
    return (
      <Badge variant="secondary" className="gap-1 font-normal">
        <CheckCircle2 aria-hidden className="size-3 text-success" />
        Configured
      </Badge>
    )
  return (
    <Badge variant="outline" className="gap-1 font-normal text-warning">
      <AlertTriangle aria-hidden className="size-3" />
      {status.state === 'unavailable' ? 'Unavailable' : 'Missing'}
    </Badge>
  )
}

type Field = {
  name: 'api_key' | 'base_url' | 'main_model' | 'local_model'
  label: string
  secret?: boolean
  placeholder?: string
  hint?: string
}

const FIELDS: Record<CredentialKind, Field[]> = {
  nebius: [
    { name: 'api_key', label: 'API key', secret: true, hint: 'Write-only: never shown again.' },
    {
      name: 'base_url',
      label: 'Provider URL',
      placeholder: NEBIUS_DEFAULT_URL,
      hint: 'https only, and only origins this server allows.',
    },
    {
      name: 'main_model',
      label: 'Main Investigator model',
      placeholder: 'nvidia/Nemotron-3-Ultra-550b-a55b',
    },
    {
      name: 'local_model',
      label: 'Local Investigator model',
      placeholder: 'nvidia/nemotron-3-super-120b-a12b',
    },
  ],
  signoz: [
    {
      name: 'base_url',
      label: 'SigNoz URL',
      placeholder: 'https://signoz.example.com',
      hint: 'Public https endpoints work. Local, private or http endpoints work only if this server’s operator allowlisted that exact origin.',
    },
    {
      name: 'api_key',
      label: 'API key (Viewer role)',
      secret: true,
      hint: 'Write-only: never shown again.',
    },
  ],
}

type Values = Record<Field['name'], string>

function initialValues(kind: CredentialKind, status: CredentialStatus | undefined): Values {
  // Model ids are configuration shown to every member, not secrets; keys and URLs are
  // never read back.
  return {
    api_key: '',
    base_url: kind === 'nebius' ? NEBIUS_DEFAULT_URL : '',
    main_model: status?.main_model ?? '',
    local_model: status?.local_model ?? '',
  }
}

function CredentialFields({
  environmentId,
  kind,
  status,
  onSaved,
  onCancel,
  onLost,
}: {
  environmentId: string
  kind: CredentialKind
  status: CredentialStatus | undefined
  onSaved: () => void
  onCancel: () => void
  // Ownership or membership ended while the form was open: the form is about to disappear.
  onLost: (message: string) => void
}) {
  const base = useId()
  const [values, setValues] = useState<Values>(() => initialValues(kind, status))
  // Not a secret: how the connection is isolated is shown to every member.
  const [isolation, setIsolation] = useState<TelemetryIsolation>(status?.isolation ?? 'dedicated')
  const [errors, setErrors] = useState<Partial<Record<Field['name'], string>>>({})
  const save = useSaveCredential(environmentId, kind)
  const replacing = status !== undefined && status.state !== 'not_configured'
  const title = `${replacing ? 'Replace' : 'Set'} ${KIND_LABEL[kind]} credentials`

  function submit(event: FormEvent) {
    event.preventDefault()
    if (save.isPending) return
    save.reset()
    const schema = kind === 'nebius' ? nebiusCredentialsInput : signozCredentialsInput
    const input =
      kind === 'nebius' ? values : { base_url: values.base_url, api_key: values.api_key, isolation }
    const parsed = schema.safeParse(input)
    if (!parsed.success) {
      // Messages are the schema's own fixed text, never the value.
      const found: Partial<Record<Field['name'], string>> = {}
      for (const issue of parsed.error.issues) {
        const name = issue.path[0] as Field['name']
        found[name] ??= issue.message
      }
      setErrors(found)
      return
    }
    setErrors({})
    save.save(parsed.data, {
      onSuccess: () => {
        setValues(initialValues(kind, undefined))
        onSaved()
      },
      onError: (error) => {
        if (lostAccess(error)) onLost(credentialError(error))
      },
    })
  }

  return (
    <form aria-label={title} onSubmit={submit} className="space-y-4" noValidate>
      <fieldset disabled={save.isPending} className="grid gap-4 sm:grid-cols-2">
        <legend className="sr-only">{title}</legend>
        {FIELDS[kind].map((f) => {
          const id = `${base}-${f.name}`
          const error = errors[f.name]
          const described = [f.hint && `${id}-hint`, error && `${id}-error`].filter(Boolean)
          return (
            <div key={f.name} className="min-w-0 space-y-1.5">
              <Label htmlFor={id}>{f.label}</Label>
              <Input
                id={id}
                name={f.name}
                type={f.secret ? 'password' : 'text'}
                autoComplete={f.secret ? 'new-password' : 'off'}
                spellCheck={false}
                autoCapitalize="none"
                placeholder={f.placeholder}
                value={values[f.name]}
                onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
                aria-invalid={error ? true : undefined}
                aria-describedby={described.length ? described.join(' ') : undefined}
                required
              />
              {f.hint && (
                <p id={`${id}-hint`} className="text-xs text-muted-foreground">
                  {f.hint}
                </p>
              )}
              {error && (
                <p id={`${id}-error`} className="text-xs text-destructive">
                  {error}
                </p>
              )}
            </div>
          )
        })}
        {kind === 'signoz' && (
          <div className="min-w-0 space-y-1.5 sm:col-span-2">
            <Label htmlFor={`${base}-isolation`}>Isolation from other organizations</Label>
            <select
              id={`${base}-isolation`}
              name="isolation"
              value={isolation}
              onChange={(e) => setIsolation(e.target.value as TelemetryIsolation)}
              aria-describedby={`${base}-isolation-hint`}
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm shadow-xs focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none dark:bg-input/30"
            >
              {(Object.keys(ISOLATION_LABEL) as TelemetryIsolation[]).map((mode) => (
                <option key={mode} value={mode}>
                  {ISOLATION_LABEL[mode]}
                </option>
              ))}
            </select>
            <p id={`${base}-isolation-hint`} className="text-xs text-muted-foreground">
              {isolation === 'dedicated'
                ? 'Your own SigNoz instance or Cloud workspace. Refused if another organization already uses it; hosted servers also need an operator to verify it.'
                : 'A SigNoz Cloud workspace shared with other organizations. The operator assigns your ingestion keys; the API key must belong to a service account whose only role is scoped to them. Saving checks that it cannot read anything else.'}
            </p>
          </div>
        )}
      </fieldset>
      {replacing && (
        <Notice>
          Replacing takes effect from the next{' '}
          {kind === 'nebius' ? 'model step' : 'telemetry query'} of running investigations; they are
          not cancelled. If the provider rejects the new values, the current credential stays.
        </Notice>
      )}
      <p className="text-xs text-muted-foreground">
        Saving checks the values with the provider first (one read-only request from the server).
        Nothing is stored unless that check passes.
      </p>
      {save.error && (
        <p
          role="alert"
          className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm"
        >
          {credentialError(save.error)}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" disabled={save.isPending}>
          {save.isPending ? (
            <Loader2 aria-hidden className="animate-spin" />
          ) : (
            <KeyRound aria-hidden />
          )}
          {save.isPending ? 'Checking with the provider…' : 'Save credentials'}
        </Button>
        <Button
          type="button"
          variant="ghost"
          disabled={save.isPending}
          onClick={() => {
            setValues(initialValues(kind, undefined))
            onCancel()
          }}
        >
          Cancel
        </Button>
      </div>
    </form>
  )
}

function RemoveCredential({
  environmentId,
  kind,
  onRemoved,
}: {
  environmentId: string
  kind: CredentialKind
  onRemoved: () => void
}) {
  const [open, setOpen] = useState(false)
  const remove = useRemoveCredential(environmentId, onRemoved)
  return (
    <>
      <Button variant="outline" onClick={() => setOpen(true)} disabled={remove.isPending}>
        {remove.isPending ? (
          <Loader2 aria-hidden className="animate-spin" />
        ) : (
          <Trash2 aria-hidden />
        )}
        Remove
      </Button>
      {remove.error && (
        <p
          role="alert"
          className="basis-full rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm"
        >
          {credentialError(remove.error)}
        </p>
      )}
      <AlertDialog open={open} onOpenChange={setOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Remove the {KIND_LABEL[kind]} credential?</AlertDialogTitle>
            <AlertDialogDescription>
              Investigations of this environment that are running now are cancelled, and new ones
              cannot start until you save a replacement. Past investigations, evidence and reports
              stay. A request already sent to the provider cannot be recalled.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep it</AlertDialogCancel>
            <AlertDialogAction variant="destructive" onClick={() => remove.mutate(kind)}>
              Remove and cancel running investigations
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

export function CredentialEditor({
  environmentId,
  kind,
  status,
  isOwner,
  ownerName,
}: {
  environmentId: string
  kind: CredentialKind
  status: CredentialStatus | undefined
  isOwner: boolean
  ownerName: string
}) {
  const [editing, setEditing] = useState(false)
  const [done, setDone] = useState<string | null>(null)
  const [lost, setLost] = useState<string | null>(null)
  if (!isOwner) {
    return (
      <div className="space-y-3">
        {lost && (
          <p
            role="alert"
            className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm"
          >
            {lost}
          </p>
        )}
        <Notice>
          Only {ownerName}, this environment’s owner, can set, replace or remove its{' '}
          {KIND_LABEL[kind]} credentials. Organization owners and admins cannot.
        </Notice>
      </div>
    )
  }
  const stored = status !== undefined && status.state !== 'not_configured'
  return (
    <div className="space-y-3">
      {done && (
        <p
          role="status"
          className="rounded-md border border-success/30 bg-success/10 px-3 py-2 text-sm"
        >
          {done}
        </p>
      )}
      {editing ? (
        <CredentialFields
          environmentId={environmentId}
          kind={kind}
          status={status}
          onSaved={() => {
            setEditing(false)
            setDone(
              stored
                ? `${KIND_LABEL[kind]} credentials replaced. Running investigations use them from their next ${kind === 'nebius' ? 'model step' : 'telemetry query'}.`
                : `${KIND_LABEL[kind]} credentials saved.`,
            )
          }}
          onCancel={() => setEditing(false)}
          onLost={(message) => {
            setEditing(false)
            setLost(message)
          }}
        />
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            onClick={() => {
              setDone(null)
              setEditing(true)
            }}
          >
            <KeyRound aria-hidden />
            {stored ? 'Replace credentials' : 'Set up credentials'}
          </Button>
          {stored && (
            <RemoveCredential
              environmentId={environmentId}
              kind={kind}
              onRemoved={() =>
                setDone(
                  `${KIND_LABEL[kind]} credential removed. Running investigations of this environment were cancelled.`,
                )
              }
            />
          )}
        </div>
      )}
    </div>
  )
}
