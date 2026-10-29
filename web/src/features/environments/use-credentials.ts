import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useRef } from 'react'

import { SESSION_KEY } from '@/app/contexts/auth'
import { ApiError, queryKeys } from '@/lib/api'
import {
  deleteCredential,
  getCredentials,
  putNebiusCredentials,
  putSignozCredentials,
  VALIDATION_MESSAGES,
  type CredentialKind,
  type NebiusCredentialsInput,
  type SignozCredentialsInput,
} from '@/lib/api/credentials'
import { TRANSFERS_KEY } from '@/lib/api/transfers'

export function useCredentials(environmentId: string, { enabled = true } = {}) {
  return useQuery({
    queryKey: queryKeys.credentials(environmentId),
    queryFn: ({ signal }) => getCredentials(environmentId, { signal }),
    enabled,
  })
}

// Everything that changes when credentials or ownership change: statuses, the detail's
// readiness and owner, permission-sensitive views, and the investigations a revocation
// cancelled.
export function useRefreshEnvironment(environmentId: string) {
  const client = useQueryClient()
  return useCallback(
    () =>
      Promise.all(
        [
          ['environment', environmentId],
          ['environments'],
          ['investigations'],
          ['investigation'],
          ['overview'],
          TRANSFERS_KEY,
        ].map((queryKey) => client.invalidateQueries({ queryKey })),
      ),
    [client, environmentId],
  )
}

type Input<K extends CredentialKind> = K extends 'nebius'
  ? NebiusCredentialsInput
  : SignozCredentialsInput

// Sets or replaces a credential. The secret is handed to the request once, through a ref
// that is emptied as soon as the request starts: it never becomes mutation variables, is
// never cached (gcTime 0) and is never retried (mutations do not retry, query-client.ts).
export function useSaveCredential<K extends CredentialKind>(environmentId: string, kind: K) {
  const pending = useRef<Input<K> | null>(null)
  const refresh = useRefreshEnvironment(environmentId)
  const client = useQueryClient()
  const mutation = useMutation({
    mutationFn: () => {
      const input = pending.current
      pending.current = null
      if (input === null) throw new Error('nothing to save')
      return kind === 'nebius'
        ? putNebiusCredentials(environmentId, input as NebiusCredentialsInput)
        : putSignozCredentials(environmentId, input as SignozCredentialsInput)
    },
    gcTime: 0,
    onError: (error) => staleCsrf(error) && client.invalidateQueries({ queryKey: SESSION_KEY }),
    onSettled: refresh,
  })
  const save = (
    input: Input<K>,
    options?: { onSuccess?: () => void; onError?: (error: Error) => void },
  ) => {
    pending.current = input
    mutation.mutate(undefined, options)
  }
  return { save, isPending: mutation.isPending, error: mutation.error, reset: mutation.reset }
}

// `onRemoved` runs at the mutation level: the refreshed status unmounts the Remove button
// before a per-call callback would run.
export function useRemoveCredential(environmentId: string, onRemoved: () => void) {
  const refresh = useRefreshEnvironment(environmentId)
  const client = useQueryClient()
  return useMutation({
    mutationFn: (kind: CredentialKind) => deleteCredential(environmentId, kind),
    onSuccess: onRemoved,
    onError: (error) => staleCsrf(error) && client.invalidateQueries({ queryKey: SESSION_KEY }),
    onSettled: refresh,
  })
}

// The caller may no longer act on the environment (ownership moved, membership ended).
export function lostAccess(error: unknown) {
  return (
    error instanceof ApiError &&
    (error.kind === 'not_found' || (error.kind === 'forbidden' && !staleCsrf(error)))
  )
}

function staleCsrf(error: unknown) {
  return error instanceof ApiError && error.kind === 'forbidden' && /csrf/i.test(error.message)
}

// What went wrong with a credential write, in fixed words. Never echoes the request, and
// the server never returns provider responses.
export function credentialError(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Something went wrong. Nothing was saved.'
  if (error.kind === 'validation') {
    if (error.code && VALIDATION_MESSAGES[error.code]) return VALIDATION_MESSAGES[error.code]!
    return `The server rejected the form: ${error.message}`
  }
  if (staleCsrf(error))
    return 'Your session’s security token was out of date. It has been refreshed; try again.'
  switch (error.kind) {
    case 'forbidden':
      return 'Only this environment’s owner can change its credentials. Ownership may have changed; the page has been refreshed.'
    case 'not_found':
      return 'This environment is no longer available to you: your membership or its organization changed.'
    case 'unauthorized':
      return 'Your session has ended. Sign in again; nothing was changed.'
    case 'unavailable':
      return 'Credential storage is not set up on this server, so credentials cannot be saved. An operator has to configure it.'
    case 'network':
      return 'The backend could not be reached. Check the status above before trying again: the change may or may not have been applied.'
    default:
      return 'The request failed. Nothing was changed.'
  }
}
