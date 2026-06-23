import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from '../App.jsx'

function okJson(body) {
  return Promise.resolve({
    ok: true,
    status: 200,
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  })
}

function okText(body) {
  return Promise.resolve({
    ok: true,
    status: 200,
    text: () => Promise.resolve(body),
  })
}

describe('App demo journey', () => {
  it('lets an operator jump from the overview into RAG, RCA, and run records', async () => {
    global.fetch = vi.fn().mockImplementation((url) => {
      const s = String(url)
      if (s.includes('/api/rca/api/v1/metrics/operations')) {
        return okJson({
          tool_call_success_rate: 0.95,
          human_acceptance_rate: 0.8,
          alert_compression_ratio: 1.5,
          report_gen_seconds_avg: 12.3,
        })
      }
      if (s.includes('/api/platform/api/v1/metrics/platform/timeseries')) {
        return okJson({ samples: [], maxlen: 120 })
      }
      if (s.includes('/api/platform/metrics')) return okText('agent_run_success_rate 1.0\n')
      if (s.includes('/api/knowledge/api/v1/documents')) return okJson({ items: [], total: 0 })
      if (s.includes('/api/rca/api/v1/rca/runs')) return okJson({ items: [], total: 0 })
      if (s.includes('/api/platform/api/v1/agent-runs')) return okJson({ items: [], total: 0 })
      return okJson({})
    })

    render(<App />)

    expect(await screen.findByText('演示流程')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /进入知识问答/ }))
    expect(await screen.findByRole('heading', { name: '知识库' })).toBeInTheDocument()

    fireEvent.click(screen.getByText('总览'))
    await waitFor(() => expect(screen.getByText('演示流程')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /查看 RCA 报告/ }))
    expect(await screen.findByRole('heading', { name: 'RCA 诊断' })).toBeInTheDocument()

    fireEvent.click(screen.getByText('总览'))
    await waitFor(() => expect(screen.getByText('演示流程')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /查看运行记录/ }))
    expect(await screen.findByRole('heading', { name: /运行实况/ })).toBeInTheDocument()
  })
})
