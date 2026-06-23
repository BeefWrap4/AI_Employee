import React, { useEffect, useMemo, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import { Button, Card, Col, Row, Statistic, Alert, Typography, Space } from 'antd'
import {
  ApartmentOutlined,
  FileSearchOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { rcaApi, platformApi } from '../api.js'

// Top-level operations dashboard. Surfaces the RCA operational metrics
// (tool success, acceptance, compression, gen time) plus a raw
// Prometheus metrics dump from the platform API, plus ECharts trend
// charts from the rolling platform metrics timeseries feed.
export default function DashboardView({ onNavigate }) {
  const [metrics, setMetrics] = useState(null)
  const [promText, setPromText] = useState('')
  const [timeseries, setTimeseries] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    const loadTimeseries = () =>
      fetch('/api/platform/api/v1/metrics/platform/timeseries', {
        headers: { Accept: 'application/json' },
      })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`timeseries ${r.status}`))))
        .then((body) => {
          if (!cancelled) setTimeseries(body)
        })
        .catch((err) => {
          if (!cancelled) setError(`Timeseries: ${err.message}`)
        })

    Promise.allSettled([rcaApi.metrics(), platformApi.metrics()]).then(
      ([rcaRes, promRes]) => {
        if (cancelled) return
        if (rcaRes.status === 'fulfilled') setMetrics(rcaRes.value)
        else setError(`RCA metrics: ${rcaRes.reason?.message}`)
        if (promRes.status === 'fulfilled') setPromText(promRes.value)
      },
    )
    loadTimeseries()
    // Light auto-refresh while the dashboard is visible.
    const t = setInterval(loadTimeseries, 15_000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [])

  // Build ECharts option objects from the timeseries samples.  Memoised so
  // each chart only recomputes when the underlying sample list changes.
  const lineOption = useMemo(() => {
    const samples = timeseries?.samples ?? []
    const labels = samples.map((s) => (s.timestamp || '').slice(11, 19))
    return {
      tooltip: { trigger: 'axis' },
      legend: { top: 4, type: 'scroll' },
      grid: { left: 48, right: 24, top: 36, bottom: 32 },
      xAxis: { type: 'category', data: labels, axisLabel: { fontSize: 11 } },
      yAxis: { type: 'value' },
      series: [
        {
          name: 'Run success rate',
          type: 'line',
          smooth: true,
          showSymbol: false,
          data: samples.map((s) => s.agent_run_success_rate ?? 0),
        },
        {
          name: 'Report acceptance rate',
          type: 'line',
          smooth: true,
          showSymbol: false,
          data: samples.map((s) => s.report_acceptance_rate ?? 0),
        },
      ],
    }
  }, [timeseries])

  const latencyOption = useMemo(() => {
    const samples = timeseries?.samples ?? []
    const labels = samples.map((s) => (s.timestamp || '').slice(11, 19))
    return {
      tooltip: { trigger: 'axis' },
      legend: { top: 4, type: 'scroll' },
      grid: { left: 56, right: 24, top: 36, bottom: 32 },
      xAxis: { type: 'category', data: labels, axisLabel: { fontSize: 11 } },
      yAxis: { type: 'value', name: 'ms' },
      series: [
        {
          name: 'Model latency p95 (ms)',
          type: 'line',
          smooth: true,
          showSymbol: false,
          areaStyle: { opacity: 0.15 },
          data: samples.map((s) => s.model_latency_p95_ms ?? 0),
        },
        {
          name: 'Tool latency p95 (ms)',
          type: 'line',
          smooth: true,
          showSymbol: false,
          areaStyle: { opacity: 0.15 },
          data: samples.map((s) => s.tool_latency_p95_ms ?? 0),
        },
      ],
    }
  }, [timeseries])

  const approvalOption = useMemo(() => {
    const samples = timeseries?.samples ?? []
    const labels = samples.map((s) => (s.timestamp || '').slice(11, 19))
    return {
      tooltip: { trigger: 'axis' },
      legend: { top: 4, type: 'scroll' },
      grid: { left: 56, right: 24, top: 36, bottom: 32 },
      xAxis: { type: 'category', data: labels, axisLabel: { fontSize: 11 } },
      yAxis: { type: 'value', name: 'seconds' },
      series: [
        {
          name: 'Approval wait p95 (s)',
          type: 'line',
          smooth: true,
          showSymbol: false,
          areaStyle: { opacity: 0.15 },
          data: samples.map((s) => s.approval_wait_time_p95_s ?? 0),
        },
        {
          name: 'Fallback rate',
          type: 'line',
          smooth: true,
          showSymbol: false,
          data: samples.map((s) => s.fallback_rate ?? 0),
        },
      ],
    }
  }, [timeseries])

  return (
    <div>
      <h2 className="page-title">平台总览</h2>
      {error && <Alert type="warning" message={error} style={{ marginBottom: 16 }} />}
      <Row gutter={16}>
        <Col span={6}>
          <Card>
            <Statistic
              title="工具调用成功率"
              value={metrics ? (metrics.tool_call_success_rate * 100).toFixed(1) : '--'}
              suffix="%"
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="人工采纳率"
              value={metrics ? (metrics.human_acceptance_rate * 100).toFixed(1) : '--'}
              suffix="%"
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="告警压缩比"
              value={metrics ? metrics.alert_compression_ratio.toFixed(2) : '--'}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="报告生成均时 (s)"
              value={metrics ? metrics.report_gen_seconds_avg.toFixed(2) : '--'}
            />
          </Card>
        </Col>
      </Row>

      <Space direction="vertical" size={16} style={{ width: '100%', marginTop: 16 }}>
        <Card title="演示流程">
          <Row gutter={[16, 16]}>
            <Col xs={24} md={8}>
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                <Typography.Text strong>1. 知识问答</Typography.Text>
                <Typography.Text type="secondary">
                  基站排障手册入库后，返回带引用证据的运维问答。
                </Typography.Text>
                <Button
                  icon={<FileSearchOutlined />}
                  onClick={() => onNavigate?.('knowledge')}
                  block
                >
                  进入知识问答
                </Button>
              </Space>
            </Col>
            <Col xs={24} md={8}>
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                <Typography.Text strong>2. RCA 报告</Typography.Text>
                <Typography.Text type="secondary">
                  告警回放收敛为 incident，生成 Top-N 根因候选和证据链。
                </Typography.Text>
                <Button
                  icon={<ApartmentOutlined />}
                  onClick={() => onNavigate?.('rca')}
                  block
                >
                  查看 RCA 报告
                </Button>
              </Space>
            </Col>
            <Col xs={24} md={8}>
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                <Typography.Text strong>3. Agent 运行</Typography.Text>
                <Typography.Text type="secondary">
                  平台侧保留 run、trace、工具调用和审批状态用于审计。
                </Typography.Text>
                <Button
                  icon={<ThunderboltOutlined />}
                  onClick={() => onNavigate?.('run')}
                  block
                >
                  查看运行记录
                </Button>
              </Space>
            </Col>
          </Row>
        </Card>
        <Card title="运行成功率趋势 (ECharts)">
          <ReactECharts
            option={lineOption}
            notMerge
            lazyUpdate
            style={{ height: 240, width: '100%' }}
            opts={{ renderer: 'canvas' }}
          />
        </Card>
        <Row gutter={16}>
          <Col span={12}>
            <Card title="模型/工具 p95 延迟">
              <ReactECharts
                option={latencyOption}
                notMerge
                lazyUpdate
                style={{ height: 240, width: '100%' }}
                opts={{ renderer: 'canvas' }}
              />
            </Card>
          </Col>
          <Col span={12}>
            <Card title="审批等待 p95 + Fallback 率">
              <ReactECharts
                option={approvalOption}
                notMerge
                lazyUpdate
                style={{ height: 240, width: '100%' }}
                opts={{ renderer: 'canvas' }}
              />
            </Card>
          </Col>
        </Row>
      </Space>

      <Card title="Prometheus 指标 (/metrics)" style={{ marginTop: 16 }}>
        <Typography.Paragraph>
          <pre style={{ maxHeight: 320, overflow: 'auto', margin: 0 }}>
            {promText || '（暂无指标）'}
          </pre>
        </Typography.Paragraph>
      </Card>
    </div>
  )
}
