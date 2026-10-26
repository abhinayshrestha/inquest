import { ArrowLeft, Loader2, Server } from 'lucide-react'
import { useId, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router'

import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { PageHeader } from '@/components/page-header'
import { SectionSkeleton } from '@/components/skeletons'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { useEnvironments } from '@/features/environments/use-environments'
import type { Severity } from '@/lib/api'
import { describeError } from '@/lib/errors'

import { useCreateInvestigation } from './queries'

const SEVERITIES: readonly Severity[] = ['low', 'medium', 'high', 'critical']
const SELECT =
  'h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 dark:bg-input/30'

export default function NewInvestigationPage() {
  const navigate = useNavigate()
  const environments = useEnvironments()
  const create = useCreateInvestigation()
  const ids = { env: useId(), title: useId(), question: useId(), severity: useId(), error: useId() }
  const [environmentId, setEnvironmentId] = useState('')
  const [title, setTitle] = useState('')
  const [question, setQuestion] = useState('')
  const [severity, setSeverity] = useState<Severity | ''>('')
  const [invalid, setInvalid] = useState<string | null>(null)

  const active = (environments.data ?? []).filter((e) => e.status === 'active')
  const selected = environmentId || (active.length === 1 ? active[0]!.id : '')

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!selected || !title.trim() || !question.trim()) {
      setInvalid('Choose an environment and enter a title and a question.')
      return
    }
    setInvalid(null)
    const { investigation, startError } = await create.mutateAsync({
      environment_id: selected,
      title: title.trim(),
      question: question.trim(),
      severity: severity || null,
    })
    navigate(`/investigations/${investigation.id}`, {
      state: startError ? { startError: describeError(startError).description } : null,
    })
  }

  let body
  if (environments.isPending) body = <SectionSkeleton label="Loading environments" />
  else if (environments.error)
    body = <ErrorState error={environments.error} onRetry={() => environments.refetch()} />
  else if (active.length === 0)
    body = (
      <EmptyState
        icon={Server}
        title="No active environment"
        description="Investigations run against an environment. Create one through the backend API first (POST /environments)."
      />
    )
  else
    body = (
      <form
        onSubmit={(e) => void submit(e).catch(() => {})}
        className="max-w-2xl space-y-5"
        noValidate
      >
        <div className="space-y-1.5">
          <Label htmlFor={ids.env}>Environment</Label>
          <select
            id={ids.env}
            className={SELECT}
            value={selected}
            onChange={(e) => setEnvironmentId(e.target.value)}
            required
          >
            {active.length > 1 && <option value="">Choose an environment…</option>}
            {active.map((e) => (
              <option key={e.id} value={e.id}>
                {e.name} ({e.type})
              </option>
            ))}
          </select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={ids.title}>Title</Label>
          <Input
            id={ids.title}
            value={title}
            maxLength={255}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="API latency on /checkout"
            required
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={ids.question}>Question</Label>
          <Textarea
            id={ids.question}
            value={question}
            maxLength={10_000}
            rows={4}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="Why is my API slow?"
            required
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={ids.severity}>Severity (optional)</Label>
          <select
            id={ids.severity}
            className={SELECT}
            value={severity}
            onChange={(e) => setSeverity(e.target.value as Severity | '')}
          >
            <option value="">None</option>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {s.charAt(0).toUpperCase() + s.slice(1)}
              </option>
            ))}
          </select>
        </div>
        {(invalid || create.error) && (
          <p id={ids.error} role="alert" className="text-sm text-destructive">
            {invalid ?? describeError(create.error).description}
          </p>
        )}
        <div className="flex gap-2">
          <Button type="submit" disabled={create.isPending}>
            {create.isPending && <Loader2 aria-hidden className="animate-spin" />}
            Create and start
          </Button>
          <Button asChild variant="ghost">
            <Link to="/investigations">Cancel</Link>
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          Starting runs the investigators against the environment&apos;s telemetry and uses model
          budget on the backend.
        </p>
      </form>
    )

  return (
    <>
      <Button asChild variant="ghost" size="sm" className="mb-2 -ml-2">
        <Link to="/investigations">
          <ArrowLeft aria-hidden />
          Investigations
        </Link>
      </Button>
      <PageHeader
        title="New investigation"
        description="Ask Inquest a question about one environment."
      />
      {body}
    </>
  )
}
