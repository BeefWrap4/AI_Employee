import React, { useEffect, useState } from 'react'
import {
  Button,
  Card,
  Col,
  Descriptions,
  List,
  Modal,
  Row,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { rcaApi } from '../api.js'

const STATUS_COLOR = {
  waiting_review: 'orange',
  accepted: 'green',
  rejected: 'red',
  need_more_evidence: 'blue',
}

// RCA diagnostic view: list runs, drill into reports, review (accept /
// reject / need-more), and import approved candidate knowledge back to
// the knowledge base.
export default function RcaView() {
  const [runs, setRuns] = useState([])
  const [selectedRun, setSelectedRun] = useState(null)
  const [report, setReport] = useState(null)
  const [candidates, setCandidates] = useState([])
  const [reviewOpen, setReviewOpen] = useState(false)
  const [reviewDecision, setReviewDecision] = useState('accepted')
  const [loading, setLoading] = useState(false)

  const loadRuns = async () => {
    try {
      const data = await rcaApi.listRuns({ page: 1, page_size: 50 })
      setRuns(data.items || [])
    } catch (e) {
      message.error(`加载运行失败: ${e.message}`)
    }
  }

  useEffect(() => {
    loadRuns()
  }, [])

  const openReport = async (run) => {
    setSelectedRun(run)
    try {
      const rep = await rcaApi.getReport(run.report_id)
      setReport(rep)
      const cands = await rcaApi.listCandidates({ incident_id: run.incident_id })
      setCandidates(cands.items || [])
    } catch (e) {
      message.error(`加载报告失败: ${e.message}`)
    }
  }

  const submitReview = async () => {
    try {
      await rcaApi.reviewReport(report.report_id, {
        decision: reviewDecision,
        final_root_cause: report.final_root_cause,
      })
      message.success('评审已提交')
      setReviewOpen(false)
      openReport(selectedRun)
    } catch (e) {
      message.error(`评审失败: ${e.message}`)
    }
  }

  const importCand = async (candidateId) => {
    try {
      await rcaApi.importCandidate(candidateId)
      message.success('候选知识已导入知识库')
    } catch (e) {
      message.error(`导入失败: ${e.message}`)
    }
  }

  const columns = [
    { title: 'Run ID', dataIndex: 'run_id', key: 'run_id' },
    { title: '事件', dataIndex: 'incident_id', key: 'incident_id' },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (s) => <Tag color={STATUS_COLOR[s] || 'default'}>{s}</Tag>,
    },
    { title: '证据数', dataIndex: 'evidence_count', key: 'evidence_count' },
    { title: '假设数', dataIndex: 'hypothesis_count', key: 'hypothesis_count' },
    {
      title: '操作',
      key: 'action',
      render: (_, run) => (
        <Button size="small" onClick={() => openReport(run)}>
          查看报告
        </Button>
      ),
    },
  ]

  return (
    <div>
      <h2 className="page-title">RCA 诊断</h2>
      <Space style={{ marginBottom: 16 }}>
        <Button onClick={loadRuns} loading={loading}>
          刷新
        </Button>
      </Space>
      <Table
        rowKey="run_id"
        columns={columns}
        dataSource={runs}
        pagination={{ pageSize: 10 }}
        size="small"
      />

      <Modal
        title={`RCA 报告 ${report?.report_id || ''}`}
        open={!!report}
        onCancel={() => {
          setReport(null)
          setSelectedRun(null)
        }}
        footer={null}
        width={900}
      >
        {report && (
          <Row gutter={16}>
            <Col span={14}>
              <Typography.Paragraph>
                <pre style={{ maxHeight: 400, overflow: 'auto', margin: 0 }}>
                  {report.report_markdown}
                </pre>
              </Typography.Paragraph>
            </Col>
            <Col span={10}>
              <Descriptions column={1} size="small" bordered>
                <Descriptions.Item label="评审状态">
                  <Tag color={STATUS_COLOR[report.review_status] || 'default'}>
                    {report.review_status}
                  </Tag>
                </Descriptions.Item>
                <Descriptions.Item label="最终根因">
                  {report.final_root_cause || '—'}
                </Descriptions.Item>
              </Descriptions>
              <Space style={{ marginTop: 12 }}>
                <Select
                  value={reviewDecision}
                  onChange={setReviewDecision}
                  style={{ width: 160 }}
                  options={[
                    { value: 'accepted', label: '采纳' },
                    { value: 'rejected', label: '驳回' },
                    { value: 'need_more_evidence', label: '需补充证据' },
                  ]}
                />
                <Button type="primary" onClick={submitReview}>
                  提交评审
                </Button>
              </Space>
              <Card title="候选知识回流" size="small" style={{ marginTop: 16 }}>
                <List
                  size="small"
                  dataSource={candidates}
                  renderItem={(c) => (
                    <List.Item
                      actions={[
                        <Button
                          size="small"
                          disabled={c.review_status !== 'approved' || c.imported_doc_id}
                          onClick={() => importCand(c.candidate_id)}
                        >
                          {c.imported_doc_id ? '已导入' : '导入'}
                        </Button>,
                      ]}
                    >
                      <List.Item.Meta
                        title={c.title}
                        description={`${c.root_cause_type} · ${c.review_status}`}
                      />
                    </List.Item>
                  )}
                  locale={{ emptyText: '无候选知识' }}
                />
              </Card>
            </Col>
          </Row>
        )}
      </Modal>
    </div>
  )
}
