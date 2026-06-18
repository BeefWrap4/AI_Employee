// Centralised API client for the web portal.
//
// Each backend service is mounted under a dev-proxy prefix (see vite.config.js):
//   /api/knowledge  -> knowledge-api       (8010)
//   /api/rca        -> rca-agent           (8020)
//   /api/platform   -> agent-platform-api  (8030)
//   /api/tools      -> tool-registry       (8040)
//
// In production the same prefixes are handled by the ingress / API gateway.
// A JWT (issued by the auth flow or a dev token) is attached as a Bearer
// header when present in localStorage.

const TOKEN_KEY = 'ai_employee_jwt'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

function authHeaders() {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function request(baseUrl, path, { method = 'GET', body, query } = {}) {
  const url = new URL(`${baseUrl}${path}`, window.location.origin)
  if (query) {
    Object.entries(query).forEach(([k, v]) => {
      if (v !== undefined && v !== null) url.searchParams.set(k, v)
    })
  }
  const resp = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: body ? JSON.stringify(body) : undefined,
  })
  const text = await resp.text()
  const data = text ? JSON.parse(text) : {}
  if (!resp.ok) {
    const err = new Error(`HTTP ${resp.status}: ${path}`)
    err.status = resp.status
    err.detail = data.detail || data
    throw err
  }
  return data
}

// --- knowledge-api ------------------------------------------------------- //
export const knowledgeApi = {
  listDocuments: (query) => request('/api/knowledge', '/api/v1/documents', { query }),
  query: (question, scopes = []) =>
    request('/api/knowledge', '/api/v1/chat/query', {
      method: 'POST',
      body: { question, knowledge_scopes: scopes },
    }),
}

// --- rca-agent ----------------------------------------------------------- //
export const rcaApi = {
  listRuns: (query) => request('/api/rca', '/api/v1/rca/runs', { query }),
  getRun: (runId) => request('/api/rca', `/api/v1/rca/runs/${runId}`),
  listReports: (query) => request('/api/rca', '/api/v1/rca/reports', { query }),
  getReport: (reportId) => request('/api/rca', `/api/v1/rca/reports/${reportId}`),
  reviewReport: (reportId, body) =>
    request('/api/rca', `/api/v1/rca/reports/${reportId}/review`, { method: 'POST', body }),
  listCandidates: (query) =>
    request('/api/rca', '/api/v1/candidate-knowledge', { query }),
  importCandidate: (candidateId) =>
    request('/api/rca', `/api/v1/candidate-knowledge/${candidateId}/import`, { method: 'POST' }),
  metrics: () => request('/api/rca', '/api/v1/metrics/operations'),
}

// --- agent-platform-api -------------------------------------------------- //
export const platformApi = {
  listTemplates: () => request('/api/platform', '/api/v1/agent-templates'),
  listRuns: (query) => request('/api/platform', '/api/v1/agent-runs', { query }),
  listEvalRuns: (query) => request('/api/platform', '/api/v1/evaluations/runs', { query }),
  compareEvals: (runA, runB) =>
    request('/api/platform', '/api/v1/evaluations/compare', { query: { run_a: runA, run_b: runB } }),
  inspect: (serviceName, checkItems) =>
    request('/api/platform', `/api/v1/inspect/${serviceName}`, {
      method: 'POST',
      query: checkItems ? { check_items: checkItems.join(',') } : undefined,
    }),
  metrics: () => fetch('/api/platform/metrics').then((r) => r.text()),
}

// --- tool-registry ------------------------------------------------------- //
export const toolsApi = {
  list: (query) => request('/api/tools', '/api/v1/tools', { query }),
  get: (name) => request('/api/tools', `/api/v1/tools/${name}`),
  invoke: (name, arguments_) =>
    request('/api/tools', `/api/v1/tools/${name}/invoke`, {
      method: 'POST',
      body: { arguments: arguments_ || {} },
    }),
}
