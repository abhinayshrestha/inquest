import type { ReactNode } from 'react'
import { ErrorState } from '@/components/error-state'
import { SectionSkeleton } from '@/components/skeletons'
import { StatusBadge } from '@/components/status-badge'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { ApiError, type Citation } from '@/lib/api'
import { formatDateTime, humanize } from '@/lib/format'
import { CitationChip } from '@/lib/render/citation-chip'
import { citationKind } from '@/lib/render/citation-kind'

import { toolLabel } from '../labels'
import { useCitation } from '../queries'
import { useCitationParam } from './use-citation-param'

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
      <dd className="text-sm">{children}</dd>
    </div>
  )
}

function Body({ citation, onCite }: { citation: Citation; onCite: (ref: string) => void }) {
  if (citation.kind === 'evidence') {
    return (
      <div className="space-y-4">
        <p className="text-sm font-medium">{citation.title}</p>
        <p className="text-sm whitespace-pre-wrap">{citation.summary}</p>
        <dl className="grid grid-cols-2 gap-3">
          <Field label="Type">{humanize(citation.evidence_type)}</Field>
          <Field label="Source">
            {citation.tool_name
              ? toolLabel(citation.tool_name)
              : humanize(citation.source_provider)}
          </Field>
          <Field label="Environment">{citation.environment.name}</Field>
          {citation.entity_identifier && (
            <Field label={citation.entity_type ? humanize(citation.entity_type) : 'Entity'}>
              {citation.entity_identifier}
            </Field>
          )}
          {citation.window_start && citation.window_end && (
            <Field label="Observation window">
              {formatDateTime(citation.window_start)} – {formatDateTime(citation.window_end)}
            </Field>
          )}
          <Field label="Collected">{formatDateTime(citation.collected_at)}</Field>
        </dl>
        {citation.redaction_applied && (
          <p className="text-xs text-muted-foreground">
            Sensitive values were redacted at collection.
          </p>
        )}
      </div>
    )
  }
  if (citation.kind === 'finding') {
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-2">
          <StatusBadge status={citation.status === 'confirmed' ? 'completed' : 'pending'} />
          <span className="text-xs text-muted-foreground">{humanize(citation.status)}</span>
        </div>
        <p className="text-sm font-medium">{citation.title}</p>
        <p className="text-sm whitespace-pre-wrap">{citation.summary}</p>
        <dl className="grid grid-cols-2 gap-3">
          <Field label="Environment">{citation.environment.name}</Field>
          <Field label="Recorded">{formatDateTime(citation.created_at)}</Field>
        </dl>
        {citation.evidence.length > 0 && (
          <div className="space-y-2">
            <p className="text-xs font-medium text-muted-foreground">Linked evidence</p>
            <ul className="space-y-1.5">
              {citation.evidence.map((e) => (
                <li key={e.ref} className="flex flex-wrap items-center gap-2 text-sm">
                  <CitationChip citationRef={e.ref} onSelect={onCite} />
                  <span className="text-xs text-muted-foreground">{humanize(e.stance)}</span>
                  <span className="min-w-0">{e.title}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    )
  }
  return (
    <div className="space-y-4">
      <p className="text-sm">{citation.statement}</p>
      <dl className="grid grid-cols-2 gap-3">
        <Field label="Status">{humanize(citation.status)}</Field>
        <Field label="Environment">{citation.environment.name}</Field>
      </dl>
      <p className="text-xs text-muted-foreground">
        Hypotheses are inferences under test, never evidence on their own.
      </p>
    </div>
  )
}

export function CitationSheet({ investigationId }: { investigationId: string }) {
  const { ref, open, close } = useCitationParam()
  const query = useCitation(investigationId, ref)
  const kind = ref ? citationKind(ref).label : 'Citation'
  return (
    <Sheet open={ref !== null} onOpenChange={(o) => !o && close()}>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-md">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">
            {kind} <span className="font-mono text-sm text-muted-foreground">{ref}</span>
          </SheetTitle>
          <SheetDescription>Recorded by Inquest during this investigation.</SheetDescription>
        </SheetHeader>
        <div className="px-4 pb-6">
          {query.isPending ? (
            <SectionSkeleton label="Loading citation" />
          ) : query.error ? (
            query.error instanceof ApiError && query.error.kind === 'not_found' ? (
              <p className="text-sm text-muted-foreground">
                This reference does not resolve to a record of this investigation.
              </p>
            ) : (
              <ErrorState error={query.error} onRetry={() => query.refetch()} />
            )
          ) : query.data.ref === ref ? (
            <Body citation={query.data} onCite={open} />
          ) : (
            <SectionSkeleton label="Loading citation" />
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
