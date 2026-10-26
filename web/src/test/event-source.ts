import { act } from '@testing-library/react'

// jsdom has no EventSource. This one records every instance so tests can push events,
// simulate the browser's own retry (readyState CONNECTING) or a give-up (CLOSED).
export class MockEventSource {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2
  static instances: MockEventSource[] = []

  readonly url: string
  readyState = MockEventSource.CONNECTING
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  private listeners = new Map<string, ((e: MessageEvent<string>) => void)[]>()

  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: (e: MessageEvent<string>) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener])
  }

  close() {
    this.readyState = MockEventSource.CLOSED
  }

  open() {
    act(() => {
      this.readyState = MockEventSource.OPEN
      this.onopen?.()
    })
  }

  emit(event: { id: number; type: string; [key: string]: unknown }) {
    const data = JSON.stringify({
      turn_id: null,
      assignment_id: null,
      created_at: '2026-10-21T17:30:00Z',
      payload: {},
      ...event,
    })
    this.raw(event.type, data, String(event.id))
  }

  raw(type: string, data: string, lastEventId = '') {
    act(() => {
      for (const listener of this.listeners.get(type) ?? []) {
        listener(new MessageEvent(type, { data, lastEventId }))
      }
    })
  }

  // Connection dropped; the browser keeps retrying by itself.
  interrupt() {
    act(() => {
      this.readyState = MockEventSource.CONNECTING
      this.onerror?.()
    })
  }

  // The browser gave up (HTTP error, proxy down): the app must reconnect itself.
  fail() {
    act(() => {
      this.readyState = MockEventSource.CLOSED
      this.onerror?.()
    })
  }

  static latest() {
    const last = MockEventSource.instances.at(-1)
    if (!last) throw new Error('no EventSource was opened')
    return last
  }

  static reset() {
    MockEventSource.instances = []
  }
}
