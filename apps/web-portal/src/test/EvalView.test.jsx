import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import EvalView from '../views/EvalView.jsx'

// Fetch dispatcher keyed by URL substring + method. The view fires two
// GETs on mount (templates + eval runs); the create form later POSTs
// /api/v1/evaluations/runs.
function makeFetch() {
  return vi.fn().mockImplementation((url, init) => {
    const s = String(url)
    const method = (init && init.method) || 'GET'
    if (s.includes('/api/v1/evaluations/runs') && method === 'POST') {
      return Promise.resolve({
        ok: true,
        status: 201,
        text: () =>
          Promise.resolve(
            JSON.stringify({
              eval_run_id: 'eval-new',
              eval_type: 'rag',
              template_id: 'knowledge_qa',
              status: 'pending',
            }),
          ),
      })
    }
    if (s.includes('/api/v1/agent-templates')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () =>
          Promise.resolve(
            JSON.stringify({
              items: [
                { template_id: 'knowledge_qa', name: 'Knowledge QA' },
                { template_id: 'rca', name: 'RCA' },
              ],
            }),
          ),
      })
    }
    // default: list eval runs
    return Promise.resolve({
      ok: true,
      status: 200,
      text: () => Promise.resolve(JSON.stringify({ items: [], total: 0 })),
    })
  })
}

describe('EvalView eval run creation', () => {
  beforeEach(() => {
    global.fetch = makeFetch()
  })
  afterEach(() => vi.restoreAllMocks())

  it('renders a create-eval-run card with eval type, template, golden path inputs', async () => {
    render(<EvalView />)
    expect(screen.getByText(/创建评测运行|新建评测/)).toBeInTheDocument()
    // eval_type select + golden_path input should be present
    expect(screen.getByText('评测类型')).toBeInTheDocument()
    expect(screen.getByPlaceholderText(/golden_path|金标路径|数据集路径/)).toBeInTheDocument()
  })

  it('POSTs createEvalRun body with eval_type, template_id, golden_path on submit', async () => {
    const fetchMock = global.fetch
    render(<EvalView />)

    // Wait for templates to load so the template Select is populated.
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/api/v1/agent-templates'))).toBe(true)
    })

    // eval_type defaults to 'rag' (set via initialValues), so we only
    // need to pick a template + fill the golden path.

    // Open the template select (the one with placeholder "选择模板") and
    // pick 'knowledge_qa'. antd Select renders a combobox; mouseDown on
    // its selector opens the dropdown.
    const templateCombobox = screen.getByRole('combobox', { name: '模板' })
    await act(async () => {
      fireEvent.mouseDown(templateCombobox)
    })
    // antd renders dropdown options as .ant-select-item-option; pick the
    // one whose text content is exactly 'knowledge_qa' (avoiding the
    // placeholder/label which also contains the string).
    const option = await waitFor(() => {
      const opts = document.querySelectorAll('.ant-select-item-option')
      const found = Array.from(opts).find((o) => o.textContent.trim() === 'knowledge_qa')
      if (!found) throw new Error('knowledge_qa option not rendered')
      return found
    })
    await act(async () => {
      fireEvent.click(option)
    })

    // Fill the golden path.
    const goldenInput = screen.getByPlaceholderText(/golden_path|金标路径|数据集路径/)
    fireEvent.change(goldenInput, { target: { value: 'tests/rca-replay/sample_cases.jsonl' } })

    // Submit. antd inserts a space between CJK chars in button text
    // ("创 建"), so match loosely.
    const submitBtn = screen.getByRole('button', { name: /创.*建/ })
    await act(async () => {
      fireEvent.click(submitBtn)
    })

    await waitFor(() => {
      const postCall = fetchMock.mock.calls.find(
        ([url, init]) =>
          String(url).includes('/api/v1/evaluations/runs') &&
          init &&
          init.method === 'POST',
      )
      expect(postCall).toBeDefined()
      const body = JSON.parse(postCall[1].body)
      expect(body.eval_type).toBe('rag')
      expect(body.template_id).toBe('knowledge_qa')
      expect(body.golden_path).toBe('tests/rca-replay/sample_cases.jsonl')
    })
  })
})
