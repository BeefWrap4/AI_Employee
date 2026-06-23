import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

// Partial mock of api.js: keep the real module exports, but spy on
// approvalApi so we can assert call args without opening a socket.
// listTasks still falls through to the real implementation (which calls
// fetch), so we also drive global.fetch for the list query.
vi.mock('../api.js', async () => {
  const actual = await vi.importActual('../api.js')
  return {
    ...actual,
    approvalApi: {
      listTasks: actual.approvalApi.listTasks,
      decide: vi.fn().mockResolvedValue({ task_id: 't1', status: 'approved' }),
      transfer: vi.fn().mockResolvedValue({ task_id: 't1', status: 'transferred' }),
      escalate: vi.fn().mockResolvedValue({ task_id: 't1', status: 'escalated' }),
    },
  }
})

import ApprovalView from '../views/ApprovalView.jsx'
import { approvalApi } from '../api.js'

// approvalApi.listTasks -> request() -> fetch().text() then JSON.parse.
function makeFetch(tasks) {
  return vi.fn().mockImplementation((url) => {
    const s = String(url)
    if (s.includes('/api/v1/approval-tasks')) {
      const body = { items: tasks, total: tasks.length }
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(JSON.stringify(body)),
        json: () => Promise.resolve(body),
      })
    }
    return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}') })
  })
}

const TASKS = [
  {
    task_id: 't1',
    run_id: 'run-1',
    template_id: 'rca',
    status: 'pending',
    risk_level: 'high_risk',
    reason: 'high-impact change',
    created_at: '2026-06-24T10:00:00Z',
  },
]

describe('ApprovalView', () => {
  afterEach(() => vi.restoreAllMocks())

  it('renders the page title and lists approval tasks in the table', async () => {
    global.fetch = makeFetch(TASKS)
    render(<ApprovalView />)
    expect(screen.getByText('审批管理')).toBeInTheDocument()
    // Row data rendered once the list resolves.
    expect(await screen.findByText('t1')).toBeInTheDocument()
    expect(screen.getByText('run-1')).toBeInTheDocument()
    expect(screen.getByText('rca')).toBeInTheDocument()
  })

  it('opens the decision modal on Approve, submits, and calls approvalApi.decide with the right body', async () => {
    global.fetch = makeFetch(TASKS)
    render(<ApprovalView />)
    await screen.findByText('t1')

    // Click the Approve action button on the row. (antd renders CJK
    // button labels with an inserted whitespace, so allow `通 过`.)
    fireEvent.click(screen.getByRole('button', { name: /通\s*过|Approve/i }))

    // Modal renders the decided_by + comment fields.
    const decidedByInput = await screen.findByPlaceholderText(/审批人|decided_by/i)
    fireEvent.change(decidedByInput, { target: { value: 'alice' } })
    const commentInput = screen.getByPlaceholderText(/备注|comment/i)
    fireEvent.change(commentInput, { target: { value: 'looks good' } })

    // Submit the modal (`提 交` due to antd CJK spacing).
    fireEvent.click(screen.getByRole('button', { name: /提\s*交|Submit|OK/i }))

    await waitFor(() => {
      expect(approvalApi.decide).toHaveBeenCalledTimes(1)
    })
    expect(approvalApi.decide).toHaveBeenCalledWith('t1', {
      decision: 'approved',
      decided_by: 'alice',
      comment: 'looks good',
    })
  })

  it('refreshes the list when the Refresh button is clicked', async () => {
    const fetchMock = makeFetch(TASKS)
    global.fetch = fetchMock
    render(<ApprovalView />)
    await screen.findByText('t1')

    // Wait for the initial load's fetch to fully drain so the next
    // click starts a clean request cycle.
    const initialCalls = fetchMock.mock.calls.length
    expect(initialCalls).toBeGreaterThanOrEqual(1)

    const refreshBtn = screen.getByRole('button', { name: /刷\s*新|Refresh/i })
    fireEvent.click(refreshBtn)

    // loadTasks is async; wait for the second fetch to land.
    await waitFor(
      () => {
        expect(fetchMock.mock.calls.length).toBeGreaterThan(initialCalls)
      },
      { timeout: 3000 },
    )
  })
})
