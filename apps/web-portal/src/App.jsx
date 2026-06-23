import React, { useState } from 'react'
import { Layout, Menu, theme as antdTheme } from 'antd'
import {
  ApartmentOutlined,
  AuditOutlined,
  ExperimentOutlined,
  FileSearchOutlined,
  ToolOutlined,
  DashboardOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import RcaView from './views/RcaView.jsx'
import KnowledgeView from './views/KnowledgeView.jsx'
import EvalView from './views/EvalView.jsx'
import ToolsView from './views/ToolsView.jsx'
import DashboardView from './views/DashboardView.jsx'
import RunView from './views/RunView.jsx'
import ApprovalView from './views/ApprovalView.jsx'

const { Header, Sider, Content } = Layout

const MENU_ITEMS = [
  { key: 'dashboard', icon: <DashboardOutlined />, label: '总览' },
  { key: 'rca', icon: <ApartmentOutlined />, label: 'RCA 诊断' },
  { key: 'knowledge', icon: <FileSearchOutlined />, label: '知识库' },
  { key: 'eval', icon: <ExperimentOutlined />, label: '评测中心' },
  { key: 'tools', icon: <ToolOutlined />, label: '工具注册' },
  { key: 'run', icon: <ThunderboltOutlined />, label: '运行实况' },
  { key: 'approval', icon: <AuditOutlined />, label: '审批管理' },
]

const VIEWS = {
  dashboard: DashboardView,
  rca: RcaView,
  knowledge: KnowledgeView,
  eval: EvalView,
  tools: ToolsView,
  run: RunView,
  approval: ApprovalView,
}

export default function App() {
  const [collapsed, setCollapsed] = useState(false)
  const [active, setActive] = useState('dashboard')
  const { token: themeToken } = antdTheme.useToken()
  const ViewComponent = VIEWS[active]

  return (
    <Layout className="portal-layout">
      <Sider collapsible collapsed={collapsed} onCollapse={setCollapsed}>
        <div
          style={{
            height: 48,
            margin: 12,
            color: '#fff',
            textAlign: 'center',
            lineHeight: '48px',
            fontWeight: 600,
          }}
        >
          {collapsed ? 'AI' : 'AI Employee'}
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[active]}
          items={MENU_ITEMS}
          onClick={({ key }) => setActive(key)}
        />
      </Sider>
      <Layout>
        <Header style={{ background: themeToken.colorBgContainer, padding: '0 24px' }}>
          <h3 style={{ margin: 0, lineHeight: '64px' }}>
            电信运维 AI 员工平台
          </h3>
        </Header>
        <Content className="portal-content">
          <ViewComponent onNavigate={setActive} />
        </Content>
      </Layout>
    </Layout>
  )
}
