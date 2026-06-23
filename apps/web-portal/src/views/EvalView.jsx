import React, { useEffect, useState } from 'react'
import {
  Button,
  Card,
  Col,
  Form,
  Input,
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
// (per-metric delta via /api/v1/evaluations/compare). Also supports
// kicking off a new eval run via POST /api/v1/evaluations/runs.
export default function EvalView() {
  const [runs, setRuns] = useState([])
  const [runA, setRunA] = useState(null)
  const [runB, setRunB] = useState(null)
  const [comparison, setComparison] = useState(null)
  const [templates, setTemplates] = useState([])
  const [creating, setCreating] = useState(false)
  const [form] = Form.useForm()

  const loadRuns = async () => {
    try {
      const data = await platformApi.listEvalRuns({ page: 1, page_size: 100 })
      setRuns(data.items || [])
    } catch (e) {
      message.error(`加载评测运行失败: ${e.message}`)
    }
  }

  const loadTemplates = async () => {
    try {
      const data = await platformApi.listTemplates()
      setTemplates(data.items || [])
    } catch (e) {
      // Non-fatal: template select just stays empty.
      setTemplates([])
    }
  }

  useEffect(() => {
    loadRuns()
    loadTemplates()
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

  const createRun = async (values) => {
    setCreating(true)
    try {
      const body = {
        eval_type: values.eval_type,
        template_id: values.template_id,
        golden_path: values.golden_path,
      }
      if (values.api_base) body.api_base = values.api_base
      await platformApi.createEvalRun(body)
      message.success('评测运行已创建')
      form.resetFields()
      await loadRuns()
    } catch (e) {
      message.error(`创建失败: ${e.message}`)
    } finally {
      setCreating(false)
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
      <Card title="创建评测运行" size="small" style={{ marginBottom: 16 }}>
        <Form
          form={form}
          layout="inline"
          onFinish={createRun}
          initialValues={{ eval_type: 'rag' }}
        >
          <Form.Item label="评测类型" name="eval_type" rules={[{ required: true }]}>
            <Select
              style={{ width: 140 }}
              options={[
                { value: 'rag', label: 'rag' },
                { value: 'rca', label: 'rca' },
              ]}
            />
          </Form.Item>
          <Form.Item label="模板" name="template_id" rules={[{ required: true }]}>
            <Select
              style={{ width: 200 }}
              placeholder="选择模板"
              options={templates.map((t) => ({
                value: t.template_id,
                label: t.template_id,
              }))}
            />
          </Form.Item>
          <Form.Item label="金标路径" name="golden_path" rules={[{ required: true }]}>
            <Input
              placeholder="golden_path 数据集路径"
              style={{ width: 280 }}
            />
          </Form.Item>
          <Form.Item label="api_base" name="api_base">
            <Input placeholder="可选，例如 http://127.0.0.1:8070" style={{ width: 240 }} />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={creating}>
              创建
            </Button>
          </Form.Item>
        </Form>
      </Card>

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
