import { ApiError } from '@/lib/api'

const MESSAGES: Record<string, { title: string; description: string }> = {
  network: {
    title: 'Backend unreachable',
    description: 'The Inquest backend did not respond. Check that it is running locally.',
  },
  unauthorized: { title: 'Sign in required', description: 'Sign in to continue.' },
  forbidden: { title: 'Not allowed', description: 'Your role does not allow this.' },
  not_found: { title: 'Not found', description: 'This resource does not exist.' },
  gone: {
    title: 'No longer valid',
    description: 'This link has expired, was revoked or was already used.',
  },
  conflict: {
    title: 'Not possible right now',
    description: 'The investigation is in a state that does not allow this action.',
  },
  unavailable: {
    title: 'Service unavailable',
    description: 'The backend reported that this capability is currently unavailable.',
  },
  validation: { title: 'Invalid request', description: 'The backend rejected the request.' },
  invalid_response: {
    title: 'Unexpected response',
    description: 'The backend response did not match the expected contract.',
  },
}

export function describeError(error: unknown) {
  if (error instanceof ApiError) {
    const known = MESSAGES[error.kind]
    // Backend details are short, deliberate messages (HTTPException detail), safe to show.
    return {
      title: known?.title ?? 'Request failed',
      description:
        error.kind === 'http' || error.kind === 'conflict' || error.kind === 'unavailable'
          ? error.message
          : (known?.description ?? error.message),
    }
  }
  return { title: 'Something went wrong', description: 'An unexpected error occurred.' }
}
