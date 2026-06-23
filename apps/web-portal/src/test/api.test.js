import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  getToken,
  setToken,
  knowledgeApi,
  rcaApi,
  platformApi,
  toolsApi,
  approvalApi,
} from '../api.js'

// --- token helpers -------------------------------------------------------- //

describe('token helpers', () => {
  beforeEach(() => localStorage.clear())

  it('getToken returns null when unset', () => {
    expect(getToken()).toBeNull()
  })

  it('setToken stores and getToken retrieves', () => {
    setToken('abc123')
    expect(getToken()).toBe('abc123')
  })

  it('setToken(null) clears the token', () => {
    setToken('abc123')
    setToken(null)
    expect(getToken()).toBeNull()
  })
})

// --- request() via fetch mock -------------------------------------------- //

function mockFetch(jsonBody, { status = 200 } = {}) {
  const text = JSON.stringify(jsonBody)
  const fetchMock = vi.fn().mockResolvedValue({
    ok: status < 400,
    status,
    text: () => Promise.resolve(text),
  })
  global.fetch = fetchMock
  return fetchMock
}

describe('knowledgeApi.query', () => {
  afterEach(() => vi.restoreAllMocks())

  it('POSTs the session, question + scopes and returns parsed body', async () => {
    localStorage.setItem('ai_employee_session_id', 'sess-test')
    const fetchMock = mockFetch({ answer: 'RRC failure means...', citations: [] })
    const result = await knowledgeApi.query('什么是 RRC?', ['ops'])
    expect(result.answer).toBe('RRC failure means...')
    expect(fetchMock).toHaveBeenCalledOnce()
    const [, init] = fetchMock.mock.calls[0]
    expect(init.method).toBe('POST')
    const body = JSON.parse(init.body)
    expect(body.session_id).toBe('sess-test')
    expect(body.question).toBe('什么是 RRC?')
    expect(body.knowledge_scopes).toEqual(['ops'])
  })

  it('attaches Bearer token when present', async () => {
    setToken('tok-xyz')
    const fetchMock = mockFetch({ ok: true })
    await knowledgeApi.query('q')
    const [, init] = fetchMock.mock.calls[0]
    expect(init.headers.Authorization).toBe('Bearer tok-xyz')
  })

  it('omits Authorization when no token', async () => {
    const fetchMock = mockFetch({ ok: true })
    await knowledgeApi.query('q')
    const [, init] = fetchMock.mock.calls[0]
    expect(init.headers.Authorization).toBeUndefined()
  })
})

describe('rcaApi', () => {
  afterEach(() => vi.restoreAllMocks())

  it('listRuns builds query string', async () => {
    const fetchMock = mockFetch({ items: [], total: 0 })
    await rcaApi.listRuns({ status: 'completed' })
    const url = fetchMock.mock.calls[0][0]
    expect(String(url)).toContain('status=completed')
  })

  it('getRun interpolates runId into path', async () => {
    const fetchMock = mockFetch({ run_id: 'r1' })
    await rcaApi.getRun('r1')
    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/v1/rca/runs/r1')
  })
})

describe('platformApi.metrics', () => {
  afterEach(() => vi.restoreAllMocks())

  it('returns raw text (Prometheus exposition)', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: () => Promise.resolve('agent_run_success_rate 1.0\n'),
    })
    const text = await platformApi.metrics()
    expect(text).toContain('agent_run_success_rate')
  })
})

describe('toolsApi.invoke', () => {
  afterEach(() => vi.restoreAllMocks())

  it('POSTs arguments body', async () => {
    const fetchMock = mockFetch({ result: 'ok' })
    await toolsApi.invoke('cmdb.lookup', { ne_id: 'BJ-001' })
    const [, init] = fetchMock.mock.calls[0]
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body).arguments).toEqual({ ne_id: 'BJ-001' })
  })
})

describe('approvalApi', () => {
  afterEach(() => vi.restoreAllMocks())

  it('listTasks GETs /api/v1/approval-tasks with query params', async () => {
    const fetchMock = mockFetch({ items: [], total: 0 })
    await approvalApi.listTasks({ status: 'pending', page: 1, page_size: 20 })
    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toContain('/api/v1/approval-tasks')
    expect(String(url)).toContain('status=pending')
    expect(init.method).toBe('GET')
  })

  it('decide POSTs to /api/v1/approval-tasks/{id}/decision with body', async () => {
    const fetchMock = mockFetch({ task_id: 't1', status: 'approved' })
    await approvalApi.decide('t1', { decision: 'approved', decided_by: 'alice', comment: 'ok' })
    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toContain('/api/v1/approval-tasks/t1/decision')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ decision: 'approved', decided_by: 'alice', comment: 'ok' })
  })

  it('requestSupplement POSTs to /api/v1/approval-tasks/{id}/supplement-request', async () => {
    const fetchMock = mockFetch({ ok: true })
    await approvalApi.requestSupplement('t2', { requested_by: 'bob', reason: 'need logs' })
    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toContain('/api/v1/approval-tasks/t2/supplement-request')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ requested_by: 'bob', reason: 'need logs' })
  })

  it('resolveSupplement POSTs to /api/v1/approval-tasks/{id}/supplement-answer', async () => {
    const fetchMock = mockFetch({ ok: true })
    await approvalApi.resolveSupplement('t3', { answered_by: 'carol', answer: 'logs attached' })
    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toContain('/api/v1/approval-tasks/t3/supplement-answer')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ answered_by: 'carol', answer: 'logs attached' })
  })

  it('transfer POSTs to /api/v1/approvals/{id}/transfer', async () => {
    const fetchMock = mockFetch({ ok: true })
    await approvalApi.transfer('t4', { new_approver: 'dave', reason: 'on leave' })
    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toContain('/api/v1/approvals/t4/transfer')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ new_approver: 'dave', reason: 'on leave' })
  })

  it('escalate POSTs to /api/v1/approvals/{id}/escalate', async () => {
    const fetchMock = mockFetch({ ok: true })
    await approvalApi.escalate('t5', { escalated_to: 'manager', reason: 'high risk' })
    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toContain('/api/v1/approvals/t5/escalate')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ escalated_to: 'manager', reason: 'high risk' })
  })

  it('delegate POSTs to /api/v1/approval-tasks/{id}/delegate', async () => {
    const fetchMock = mockFetch({ ok: true })
    await approvalApi.delegate('t6', { delegate_to: 'erin', reason: 'delegation' })
    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toContain('/api/v1/approval-tasks/t6/delegate')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ delegate_to: 'erin', reason: 'delegation' })
  })
})

// --- error path ----------------------------------------------------------- //

describe('error handling', () => {
  afterEach(() => vi.restoreAllMocks())

  it('throws with status + detail on non-2xx', async () => {
    mockFetch({ detail: 'not found' }, { status: 404 })
    await expect(knowledgeApi.query('q')).rejects.toMatchObject({
      status: 404,
      detail: 'not found',
    })
  })
})
