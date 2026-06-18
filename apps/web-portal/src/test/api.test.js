import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  getToken,
  setToken,
  knowledgeApi,
  rcaApi,
  platformApi,
  toolsApi,
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

  it('POSTs the question + scopes and returns parsed body', async () => {
    const fetchMock = mockFetch({ answer: 'RRC failure means...', citations: [] })
    const result = await knowledgeApi.query('什么是 RRC?', ['ops'])
    expect(result.answer).toBe('RRC failure means...')
    expect(fetchMock).toHaveBeenCalledOnce()
    const [, init] = fetchMock.mock.calls[0]
    expect(init.method).toBe('POST')
    const body = JSON.parse(init.body)
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
