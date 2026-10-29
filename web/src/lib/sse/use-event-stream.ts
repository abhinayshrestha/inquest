import { useEffect, useReducer, useRef, useState } from 'react'

import { eventsUrl, progressEvent, type ProgressEvent } from '@/lib/api'

import { EVENT_TYPES } from './events'

// revoked: the server ended the stream because the session or membership is gone.
export type StreamStatus = 'connecting' | 'open' | 'reconnecting' | 'closed' | 'revoked'

const MAX_BACKOFF_MS = 15_000
const BASE_BACKOFF_MS = 1_000

type State = { events: ProgressEvent[]; lastId: number }

// Event ids are the backend's strictly increasing replay cursor, so anything at or below
// the last id seen is a duplicate (a replay after reconnect).
function reducer(state: State, incoming: ProgressEvent[]): State {
  const fresh = incoming.filter((e) => e.id > state.lastId).sort((a, b) => a.id - b.id)
  if (fresh.length === 0) return state
  return { events: [...state.events, ...fresh], lastId: fresh[fresh.length - 1]!.id }
}

/**
 * Live progress events of one investigation.
 *
 * While the connection is merely interrupted, the browser reconnects by itself and sends
 * Last-Event-ID, which the backend replays from. When the EventSource gives up (closed:
 * HTTP error, proxy failure), a new one is opened with ?after=<last id>, with exponential
 * backoff. Malformed events are dropped, never rendered. An access_revoked event (session
 * ended or membership removed) closes the stream for good: reconnecting cannot succeed.
 */
export function useEventStream(
  investigationId: string | null,
  onEvent?: (event: ProgressEvent) => void,
) {
  const [state, dispatch] = useReducer(reducer, { events: [], lastId: 0 })
  const [status, setStatus] = useState<StreamStatus>('connecting')
  const lastIdRef = useRef(0)
  const onEventRef = useRef(onEvent)

  useEffect(() => {
    onEventRef.current = onEvent
  }, [onEvent])

  useEffect(() => {
    lastIdRef.current = state.lastId
  }, [state.lastId])

  useEffect(() => {
    if (!investigationId) return
    let source: EventSource | null = null
    let retryTimer: ReturnType<typeof setTimeout> | undefined
    let attempts = 0
    let disposed = false

    const handle = (message: MessageEvent<string>) => {
      let data: unknown
      try {
        data = JSON.parse(message.data)
      } catch {
        return
      }
      const parsed = progressEvent.safeParse(data)
      if (!parsed.success || parsed.data.id <= lastIdRef.current) return
      lastIdRef.current = parsed.data.id
      dispatch([parsed.data])
      onEventRef.current?.(parsed.data)
    }

    const connect = () => {
      if (disposed) return
      const after = lastIdRef.current
      source = new EventSource(eventsUrl(investigationId, after > 0 ? after : undefined))
      for (const type of EVENT_TYPES) source.addEventListener(type, handle)
      source.addEventListener('access_revoked', () => {
        disposed = true
        source?.close()
        setStatus('revoked')
      })
      source.onopen = () => {
        attempts = 0
        setStatus('open')
      }
      source.onerror = () => {
        if (disposed) return
        if (!source || source.readyState !== EventSource.CLOSED) {
          // The browser is retrying on its own with Last-Event-ID.
          setStatus('reconnecting')
          return
        }
        source.close()
        setStatus('reconnecting')
        const delay = Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * 2 ** attempts)
        attempts += 1
        retryTimer = setTimeout(connect, delay)
      }
    }

    connect()
    return () => {
      disposed = true
      clearTimeout(retryTimer)
      source?.close()
      setStatus((current) => (current === 'revoked' ? current : 'closed'))
    }
  }, [investigationId])

  return { events: state.events, status }
}
