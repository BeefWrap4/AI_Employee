import React, { useEffect, useState } from 'react'
import { Card, Col, Row, Statistic, Alert, Typography } from 'antd'
import { rcaApi, platformApi } from '../api.js'

// Top-level operations dashboard. Surfaces the RCA operational metrics
// (tool success, acceptance, compression, gen time) plus a raw
// Prometheus metrics dump from the platform API.
export default function DashboardView() {
  const [metrics, setMetrics] = useState(null)
  const [promText, setPromText] = useState('')
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    Promise.allSettled([rcaApi.metrics(), platformApi.metrics()]).then(
      ([rcaRes, promRes]) => {
        if (cancelled) return
        if (rcaRes.status === 'fulfilled') setMetrics(rcaRes.value)
        else setError(`RCA metrics: ${rcaRes.reason?.message}`)
        if (promRes.status === 'fulfilled') setPromText(promRes.value)
      },
    )
    return () => {
      cancelled = true
    }
  }, [])

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
