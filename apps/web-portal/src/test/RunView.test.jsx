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

  it('creates a run from the form and subscribes to the new run stream', async () => {
    // R36-A: the create-run form POSTs to /api/v1/agent-runs then
    // auto-subscribes to the SSE stream of the returned run_id.
    const createdBody = {
      run_id: 'run_x',
      template_id: 'knowledge_qa',
      agent_name: '知识问答 Agent',
      status: 'running',
      trace_id: 'trace-x',
      requested_by: 'tester',
      input: { query: '什么是 RRC?' },
      output: {},
      node_trace: [],
      tool_calls: [],
    }
    global.fetch = vi.fn().mockImplementation((url) => {
      const s = String(url)
      if (s.endsWith('/api/platform/api/v1/agent-templates')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          text: () =>
            Promise.resolve(
              JSON.stringify({
                items: [
                  { template_id: 'knowledge_qa', agent_name: '知识问答 Agent', version: '1' },
                  { template_id: 'rca', agent_name: 'RCA Agent', version: '1' },
                ],
                total: 2,
              }),
            ),
        })
      }
      if (
        s.endsWith('/api/platform/api/v1/agent-runs?page=1&page_size=20')
      ) {
        return Promise.resolve({
          ok: true,
          status: 200,
          text: () => Promise.resolve(JSON.stringify({ items: [], total: 0 })),
        })
      }
      if (s.endsWith('/api/platform/api/v1/agent-runs')) {
        return Promise.resolve({
          ok: true,
          status: 201,
          text: () => Promise.resolve(JSON.stringify(createdBody)),
        })
      }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') })
    })

    const { user } = makeUser()
    render(<RunView />)

    // Templates load into the Select.
    await waitFor(() => {
      const templatesCall = global.fetch.mock.calls.find(([u]) =>
        String(u).includes('/api/v1/agent-templates'),
      )
      expect(templatesCall).toBeTruthy()
    })

    // Open the template Select and pick knowledge_qa. Antd renders the
    // dropdown in a portal; the proven jsdom pattern is mouseDown on the
    // selector then click the option by its title attribute.
    const templateSelector = document.querySelector('.ant-select-selector')
    fireEvent.mouseDown(templateSelector)
    await waitFor(() => {
      expect(screen.getByTitle('knowledge_qa')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTitle('knowledge_qa'))

    // Fill requested_by and the query input.
    await user.type(screen.getByPlaceholderText(/requested_by/), 'tester')
    await user.type(screen.getByPlaceholderText(/输入问题|query/i), '什么是 RRC?')

    // Submit the create-run form.
    await user.click(screen.getByRole('button', { name: /创建|Create|启动|提交/ }))

    // platformApi.createRun was POSTed with the right body.
    await waitFor(() => {
      const createCall = global.fetch.mock.calls.find(
        ([u, init]) =>
          String(u).endsWith('/api/platform/api/v1/agent-runs') &&
          init &&
          init.method === 'POST',
      )
      expect(createCall).toBeTruthy()
      const body = JSON.parse(createCall[1].body)
      expect(body.template_id).toBe('knowledge_qa')
      expect(body.requested_by).toBe('tester')
      expect(body.input).toEqual({ query: '什么是 RRC?' })
    })

    // Auto-subscribed to the new run's SSE stream.
    await waitFor(() => {
      expect(MockEventSource.instances).toHaveLength(1)
    })
    expect(MockEventSource.instances[0].url).toBe(
      '/api/platform/api/v1/agent-runs/run_x/stream',
    )
  })
})
