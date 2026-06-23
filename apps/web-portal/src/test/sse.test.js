import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { subscribeRunStream } from '../sse.js'

// A minimal EventSource mock. Records the URL it was constructed with,
// exposes onmessage/onerror hooks, tracks close(), and gives the test
// a .push(raw) helper to simulate the browser delivering an SSE
// "data: {json}\n\n" frame.
//
// The real browser EventSource parses the stream and fires onmessage
// with event.data set to the frame's data payload (the JSON string).
// We mirror that contract: push(jsonString) sets message event.data
// and invokes onmessage.
class MockEventSource {
  constructor(url) {
    this.url = url
    this.readyState = 0 // CONNECTING
    this.onmessage = null
    this.onerror = null
    this.onopen = null
    this.closed = false
    MockEventSource.instances.push(this)
  }
  // Test helper: deliver one SSE frame's data payload.
  push(data) {
    if (this.onmessage) this.onmessage({ data })
  }
  // Test helper: deliver an error (e.g. stream closed / network).
  error() {
    this.readyState = 2
    if (this.onerror) this.onerror(new Event('error'))
  }
  close() {
    this.closed = true
    this.readyState = 2
  }
}

describe('subscribeRunStream', () => {
  beforeEach(() => {
    MockEventSource.instances = []
    global.EventSource = MockEventSource
  })
  afterEach(() => {
    vi.restoreAllMocks()
    delete global.EventSource
  })

  it('opens an EventSource at the platform stream URL', () => {
    subscribeRunStream('run-42', () => {})
    expect(MockEventSource.instances).toHaveLength(1)
    expect(MockEventSource.instances[0].url).toBe(
      '/api/platform/api/v1/agent-runs/run-42/stream',
    )
  })

  it('parses each SSE message as JSON and calls onEvent with the event', () => {
    const events = []
    subscribeRunStream('run-1', (ev) => events.push(ev))
    const es = MockEventSource.instances[0]

    es.push(JSON.stringify({ run_id: 'run-1', event_type: 'started', payload: {}, ts: 't1' }))
    es.push(
      JSON.stringify({
        run_id: 'run-1',
        event_type: 'tool_call',
        payload: { name: 'cmdb.lookup' },
        ts: 't2',
      }),
    )

    expect(events).toHaveLength(2)
    expect(events[0]).toEqual({ run_id: 'run-1', event_type: 'started', payload: {}, ts: 't1' })
    expect(events[1].event_type).toBe('tool_call')
    expect(events[1].payload).toEqual({ name: 'cmdb.lookup' })
  })

  it('returns a cleanup function that closes the EventSource', () => {
    const cleanup = subscribeRunStream('run-2', () => {})
    const es = MockEventSource.instances[0]
    expect(es.closed).toBe(false)
    cleanup()
    expect(es.closed).toBe(true)
  })

  it('calls onError and closes when the stream errors', () => {
    const onError = vi.fn()
    subscribeRunStream('run-3', () => {}, onError)
    const es = MockEventSource.instances[0]
    es.error()
    expect(onError).toHaveBeenCalledOnce()
    expect(es.closed).toBe(true)
  })

  it('does not throw when onError is omitted and the stream errors', () => {
    const cleanup = subscribeRunStream('run-4', () => {})
    const es = MockEventSource.instances[0]
    expect(() => es.error()).not.toThrow()
    expect(es.closed).toBe(true)
    cleanup()
  })
})
