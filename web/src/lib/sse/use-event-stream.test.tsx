import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { INVESTIGATION_ID, TURN_1 } from '@/test/fixtures'
import { MockEventSource } from '@/test/event-source'

import { useEventStream } from './use-event-stream'

const BASE = `/api/investigations/${INVESTIGATION_ID}/events`

describe('useEventStream', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('connects from the beginning and subscribes to named events', () => {
    const onEvent = vi.fn()
    const { result } = renderHook(() => useEventStream(INVESTIGATION_ID, onEvent))
    const source = MockEventSource.latest()
    expect(source.url).toBe(BASE)

    source.open()
    source.emit({ id: 1, type: 'turn.queued', turn_id: TURN_1 })
    source.emit({ id: 2, type: 'turn.started', turn_id: TURN_1 })

    expect(result.current.status).toBe('open')
    expect(result.current.events.map((e) => e.id)).toEqual([1, 2])
    expect(onEvent).toHaveBeenCalledTimes(2)
  })

  it('drops duplicates and malformed events', () => {
    const { result } = renderHook(() => useEventStream(INVESTIGATION_ID))
    const source = MockEventSource.latest()
    source.emit({ id: 5, type: 'turn.started' })
    source.emit({ id: 5, type: 'turn.started' }) // replayed
    source.emit({ id: 3, type: 'turn.queued' }) // older than what was seen
    source.raw('turn.completed', 'not json')
    source.raw('turn.completed', JSON.stringify({ id: 'x', type: 'turn.completed' }))
    expect(result.current.events.map((e) => e.id)).toEqual([5])
  })

  it('leaves interrupted connections to the browser (Last-Event-ID retry)', () => {
    const { result } = renderHook(() => useEventStream(INVESTIGATION_ID))
    const source = MockEventSource.latest()
    source.emit({ id: 1, type: 'turn.started' })
    source.interrupt()
    expect(result.current.status).toBe('reconnecting')
    act(() => vi.advanceTimersByTime(30_000))
    expect(MockEventSource.instances).toHaveLength(1)
  })

  it('reopens a closed stream after the last seen id, with backoff', () => {
    const { result } = renderHook(() => useEventStream(INVESTIGATION_ID))
    const first = MockEventSource.latest()
    first.emit({ id: 7, type: 'turn.started' })
    first.fail()
    expect(first.readyState).toBe(MockEventSource.CLOSED)

    act(() => vi.advanceTimersByTime(999))
    expect(MockEventSource.instances).toHaveLength(1)
    act(() => vi.advanceTimersByTime(1))
    const second = MockEventSource.latest()
    expect(second.url).toBe(`${BASE}?after=7`)

    // The replay overlaps what was already seen; only new ids are added.
    second.emit({ id: 7, type: 'turn.started' })
    second.emit({ id: 8, type: 'turn.completed' })
    expect(result.current.events.map((e) => e.id)).toEqual([7, 8])

    second.fail()
    act(() => vi.advanceTimersByTime(1_999))
    expect(MockEventSource.instances).toHaveLength(2)
    act(() => vi.advanceTimersByTime(1))
    expect(MockEventSource.latest().url).toBe(`${BASE}?after=8`)
  })

  it('closes the stream on unmount and does not reconnect', () => {
    const { unmount } = renderHook(() => useEventStream(INVESTIGATION_ID))
    const source = MockEventSource.latest()
    source.fail()
    unmount()
    act(() => vi.advanceTimersByTime(60_000))
    expect(MockEventSource.instances).toHaveLength(1)
    expect(source.readyState).toBe(MockEventSource.CLOSED)
  })
})
