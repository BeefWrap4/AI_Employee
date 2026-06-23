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
const SESSION_KEY = 'ai_employee_session_id'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

function getSessionId() {
  let sessionId = localStorage.getItem(SESSION_KEY)
  if (!sessionId) {
    sessionId = `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
    localStorage.setItem(SESSION_KEY, sessionId)
  }
  return sessionId
}

function authHeaders() {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

// When `isForm` is true the caller passes a FormData instance as `body`;
// the browser sets the multipart Content-Type (with boundary) itself, so
// we must NOT pin application/json or JSON.stringify the body.
async function request(baseUrl, path, { method = 'GET', body, query, isForm = false } = {}) {
  const url = new URL(`${baseUrl}${path}`, window.location.origin)
  if (query) {
    Object.entries(query).forEach(([k, v]) => {
      if (v !== undefined && v !== null) url.searchParams.set(k, v)
    })
  }
  const headers = isForm ? { ...authHeaders() } : { 'Content-Type': 'application/json', ...authHeaders() }
  const resp = await fetch(url, {
    method,
    headers,
    body: body ? (isForm ? body : JSON.stringify(body)) : undefined,
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
      body: { session_id: getSessionId(), question, knowledge_scopes: scopes },
    }),
  // Multipart upload: the backend POST /api/v1/documents expects file +
  // title + metadata_json + acl_tags_json + version fields.
  uploadDocument: (file, title, metadata = {}, aclTags = []) => {
    const form = new FormData()
    form.append('file', file)
    form.append('title', title)
    form.append('metadata_json', JSON.stringify(metadata))
    form.append('acl_tags_json', JSON.stringify(aclTags))
    form.append('version', 'v1')
    return request('/api/knowledge', '/api/v1/documents', {
      method: 'POST',
      body: form,
      isForm: true,
    })
  },
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
  // R36-A: start a new agent run. body = {template_id, requested_by, input}.
  // The request() helper attaches the X-Internal-Token / Bearer header.
  createRun: (body) => request('/api/platform', '/api/v1/agent-runs', { method: 'POST', body }),
  getRunTrace: (runId) => request('/api/platform', `/api/v1/agent-runs/${runId}/trace`),
  listEvalRuns: (query) => request('/api/platform', '/api/v1/evaluations/runs', { query }),
  createEvalRun: (body) =>
    request('/api/platform', '/api/v1/evaluations/runs', { method: 'POST', body }),
  compareEvals: (runA, runB) =>
    request('/api/platform', '/api/v1/evaluations/compare', { query: { run_a: runA, run_b: runB } }),
  inspect: (serviceName, checkItems) =>
    request('/api/platform', `/api/v1/inspect/${serviceName}`, {
      method: 'POST',
      query: checkItems ? { check_items: checkItems.join(',') } : undefined,
    }),
  metrics: () => fetch('/api/platform/metrics').then((r) => r.text()),
}

// --- approval tasks (agent-platform-api) -------------------------------- //
export const approvalApi = {
  listTasks: (query) => request('/api/platform', '/api/v1/approval-tasks', { query }),
  decide: (taskId, body) =>
    request('/api/platform', `/api/v1/approval-tasks/${taskId}/decision`, {
      method: 'POST',
      body,
    }),
  requestSupplement: (taskId, body) =>
    request('/api/platform', `/api/v1/approval-tasks/${taskId}/supplement-request`, {
      method: 'POST',
      body,
    }),
  resolveSupplement: (taskId, body) =>
    request('/api/platform', `/api/v1/approval-tasks/${taskId}/supplement-answer`, {
      method: 'POST',
      body,
    }),
  transfer: (taskId, body) =>
    request('/api/platform', `/api/v1/approvals/${taskId}/transfer`, { method: 'POST', body }),
  escalate: (taskId, body) =>
    request('/api/platform', `/api/v1/approvals/${taskId}/escalate`, { method: 'POST', body }),
  delegate: (taskId, body) =>
    request('/api/platform', `/api/v1/approval-tasks/${taskId}/delegate`, {
      method: 'POST',
      body,
    }),
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
