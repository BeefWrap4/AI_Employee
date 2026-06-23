import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import DashboardView from '../views/DashboardView.jsx'

// DashboardView fires three network calls on mount:
//   1. rcaApi.metrics()         -> request() -> fetch(...).text() then JSON.parse
//   2. platformApi.metrics()    -> fetch(...).then(r => r.text())
//   3. fetch('/api/platform/api/v1/metrics/platform/timeseries') -> r.json()
//
// We stub global.fetch with a single handler that dispatches by URL.

function makeFetch() {
  return vi.fn().mockImplementation((url) => {
    const s = String(url)
    if (s.includes('timeseries')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(JSON.stringify({ samples: [], maxlen: 120 })),
        json: () => Promise.resolve({ samples: [], maxlen: 120 }),
      })
    }
    if (s.includes('/api/rca')) {
      // rcaApi.metrics() -> request() reads .text() then JSON.parses
      const body = {
        tool_call_success_rate: 0.95,
        human_acceptance_rate: 0.8,
        alert_compression_ratio: 1.5,
        report_gen_seconds_avg: 12.3,
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(JSON.stringify(body)),
        json: () => Promise.resolve(body),
      })
    }
    // platformApi.metrics() -> fetch(...).then(r => r.text())
    return Promise.resolve({
      ok: true,
      status: 200,
      text: () => Promise.resolve('agent_run_success_rate 1.0\n'),
    })
  })
}

describe('DashboardView', () => {
  afterEach(() => vi.restoreAllMocks())

  it('renders the page title and metric card titles', async () => {
    global.fetch = makeFetch()
    render(<DashboardView />)
    expect(screen.getByText('平台总览')).toBeInTheDocument()
    expect(screen.getByText('工具调用成功率')).toBeInTheDocument()
    expect(screen.getByText('人工采纳率')).toBeInTheDocument()
    expect(screen.getByText('告警压缩比')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByText('95')).toBeInTheDocument()
    })
  })

  it('populates metric values once data resolves', async () => {
    global.fetch = makeFetch()
    render(<DashboardView />)
    // 工具调用成功率 = 0.95 * 100 = 95 once rcaApi.metrics resolves
    // (antd Statistic renders value + '%' suffix as siblings).
    await waitFor(() => {
      expect(screen.getByText('95')).toBeInTheDocument()
    })
  })

  it('renders the Prometheus dump card', async () => {
    global.fetch = makeFetch()
    render(<DashboardView />)
    await waitFor(() => {
      expect(screen.getByText(/Prometheus 指标/)).toBeInTheDocument()
    })
  })

  it('renders the ECharts trend card', async () => {
    global.fetch = makeFetch()
    render(<DashboardView />)
    await waitFor(() => {
      expect(screen.getByText('运行成功率趋势 (ECharts)')).toBeInTheDocument()
    })
  })
})
