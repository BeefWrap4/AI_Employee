import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import RunView from '../views/RunView.jsx'

// @testing-library/user-event isn't in devDeps; a tiny user helper
// backed by fireEvent (which antd Input/Button react to) is enough.
function makeUser() {
  return {
    user: {
      type: (el, text) => {
        fireEvent.change(el, { target: { value: text } })
        return Promise.resolve()
      },
      click: (el) => {
        fireEvent.click(el)
        return Promise.resolve()
      },
    },
  }
}

// Mock EventSource matching the contract used by src/sse.js.
// push(dataJsonString) fires onmessage; error() fires onerror; close() is tracked.
class MockEventSource {
  constructor(url) {
    this.url = url
    this.onmessage = null
    this.onerror = null
    this.closed = false
    MockEventSource.instances.push(this)
  }
  push(data) {
    if (this.onmessage) this.onmessage({ data })
  }
  error() {
    if (this.onerror) this.onerror(new Event('error'))
  }
  close() {
    this.closed = true
  }
}

describe('RunView', () => {
  beforeEach(() => {
    MockEventSource.instances = []
    global.EventSource = MockEventSource
  })
  afterEach(() => {
    vi.restoreAllMocks()
    delete global.EventSource
  })

  it('renders the page title and a watch control', () => {
    render(<RunView />)
    expect(screen.getByText(/运行实况|Run Live/)).toBeInTheDocument()
  })

  it('subscribes to the run stream when a run id is entered and Watch clicked', async () => {
    const { user } = makeUser()
    render(<RunView />)
    expect(MockEventSource.instances).toHaveLength(0)

    await user.type(screen.getByPlaceholderText(/run_id|运行 ID/), 'run-abc')
    await user.click(screen.getByRole('button', { name: /Watch|观看/ }))

    await waitFor(() => {
      expect(MockEventSource.instances).toHaveLength(1)
    })
    expect(MockEventSource.instances[0].url).toBe(
      '/api/platform/api/v1/agent-runs/run-abc/stream',
    )
  })

  it('renders streamed events in the timeline newest-first', async () => {
    const { user } = makeUser()
    render(<RunView />)
    await user.type(screen.getByPlaceholderText(/run_id|运行 ID/), 'run-1')
    await user.click(screen.getByRole('button', { name: /Watch|观看/ }))

    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1))
    const es = MockEventSource.instances[0]

    await act(async () => {
      es.push(
        JSON.stringify({ run_id: 'run-1', event_type: 'started', payload: { node: 'plan' }, ts: 't1' }),
      )
    })
    await act(async () => {
      es.push(
        JSON.stringify({
          run_id: 'run-1',
          event_type: 'tool_call',
          payload: { name: 'cmdb.lookup' },
          ts: 't2',
        }),
      )
    })

    // Both event types should render.
    expect(await screen.findByText('tool_call')).toBeInTheDocument()
    expect(screen.getByText('started')).toBeInTheDocument()
    // Newest-first: tool_call should appear before started in the DOM.
    const types = screen.getAllByText(/started|tool_call/).map((el) => el.textContent)
    expect(types.indexOf('tool_call')).toBeLessThan(types.indexOf('started'))
  })

  it('closes the EventSource on unmount', async () => {
    const { user } = makeUser()
    const { unmount } = render(<RunView />)
    await user.type(screen.getByPlaceholderText(/run_id|运行 ID/), 'run-2')
    await user.click(screen.getByRole('button', { name: /Watch|观看/ }))
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1))
    const es = MockEventSource.instances[0]
    expect(es.closed).toBe(false)
    unmount()
    expect(es.closed).toBe(true)
  })

  it('shows connected status while subscribed', async () => {
    const { user } = makeUser()
    render(<RunView />)
    await user.type(screen.getByPlaceholderText(/run_id|运行 ID/), 'run-3')
    await user.click(screen.getByRole('button', { name: /Watch|观看/ }))
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1))
    expect(screen.getByText(/已连接|connected/i)).toBeInTheDocument()
  })
})
