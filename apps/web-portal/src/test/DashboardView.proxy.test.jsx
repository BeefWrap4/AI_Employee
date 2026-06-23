import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import DashboardView from '../views/DashboardView.jsx'

function makeFetch() {
  return vi.fn().mockImplementation((url) => {
    const s = String(url)
    if (s.includes('/api/rca')) {
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
    if (s.includes('timeseries')) {
      const body = { samples: [], maxlen: 120 }
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(JSON.stringify(body)),
        json: () => Promise.resolve(body),
      })
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      text: () => Promise.resolve('agent_run_success_rate 1.0\n'),
    })
  })
}

describe('DashboardView API proxy paths', () => {
  afterEach(() => vi.restoreAllMocks())

  it('requests timeseries data through the platform API proxy prefix', async () => {
    const fetchMock = makeFetch()
    global.fetch = fetchMock
    render(<DashboardView />)
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/platform/api/v1/metrics/platform/timeseries',
        expect.objectContaining({ headers: { Accept: 'application/json' } }),
      )
    })
  })
})
