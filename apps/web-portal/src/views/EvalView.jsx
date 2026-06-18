import React, { useEffect, useState } from 'react'
import {
  Button,
  Card,
  Col,
  Row,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { platformApi } from '../api.js'

// Eval center: list eval runs and compare two runs side by side
// (per-metric delta via /api/v1/evaluations/compare).
export default function EvalView() {
  const [runs, setRuns] = useState([])
  const [runA, setRunA] = useState(null)
  const [runB, setRunB] = useState(null)
  const [comparison, setComparison] = useState(null)

  const loadRuns = async () => {
    try {
      const data = await platformApi.listEvalRuns({ page: 1, page_size: 100 })
      setRuns(data.items || [])
    } catch (e) {
      message.error(`加载评测运行失败: ${e.message}`)
    }
  }

  useEffect(() => {
    loadRuns()
  }, [])

  const compare = async () => {
    if (!runA || !runB) {
      message.warning('请选择两个运行进行对比')
      return
    }
    try {
      const result = await platformApi.compareEvals(runA, runB)
      setComparison(result)
    } catch (e) {
      message.error(`对比失败: ${e.message}`)
    }
  }

  const runColumns = [
    { title: 'Eval Run', dataIndex: 'eval_run_id', key: 'eval_run_id' },
    { title: '类型', dataIndex: 'eval_type', key: 'eval_type' },
    { title: '模板', dataIndex: 'template_id', key: 'template_id' },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (s) => (
        <Tag color={s === 'completed' ? 'green' : s === 'failed' ? 'red' : 'orange'}>
          {s}
        </Tag>
      ),
    },
    { title: '完成时间', dataIndex: 'completed_at', key: 'completed_at' },
  ]

  const deltaColumns = [
    { title: '指标', dataIndex: 'metric', key: 'metric' },
    {
      title: 'A',
      dataIndex: 'a',
      key: 'a',
      render: (v) => (v === null ? '—' : Number(v).toFixed(4)),
    },
    {
      title: 'B',
      dataIndex: 'b',
      key: 'b',
      render: (v) => (v === null ? '—' : Number(v).toFixed(4)),
    },
    {
      title: '差值',
      dataIndex: 'delta',
      key: 'delta',
      render: (v) =>
        v === null ? '—' : (
          <Tag color={v > 0 ? 'green' : v < 0 ? 'red' : 'default'}>
            {v > 0 ? '+' : ''}
            {Number(v).toFixed(4)}
          </Tag>
        ),
    },
    { title: '方向', dataIndex: 'direction', key: 'direction' },
  ]

  return (
    <div>
      <h2 className="page-title">评测中心</h2>
      <Card title="版本对比" size="small" style={{ marginBottom: 16 }}>
        <Space>
          <Select
            placeholder="运行 A"
            style={{ width: 220 }}
            value={runA}
            onChange={setRunA}
            options={runs.map((r) => ({ value: r.eval_run_id, label: r.eval_run_id }))}
          />
          <Select
            placeholder="运行 B"
            style={{ width: 220 }}
            value={runB}
            onChange={setRunB}
            options={runs.map((r) => ({ value: r.eval_run_id, label: r.eval_run_id }))}
          />
          <Button type="primary" onClick={compare}>
            对比
          </Button>
        </Space>
        {comparison && (
          <Row gutter={16} style={{ marginTop: 16 }}>
            <Col span={24}>
              <Typography.Text>
                {comparison.a.eval_run_id} → {comparison.b.eval_run_id}
              </Typography.Text>
              <Table
                rowKey="metric"
                columns={deltaColumns}
                dataSource={comparison.metrics}
                pagination={false}
                size="small"
                style={{ marginTop: 8 }}
              />
            </Col>
          </Row>
        )}
      </Card>

      <Card title="评测运行" size="small">
        <Table
          rowKey="eval_run_id"
          columns={runColumns}
          dataSource={runs}
          pagination={{ pageSize: 10 }}
          size="small"
        />
      </Card>
    </div>
  )
}
