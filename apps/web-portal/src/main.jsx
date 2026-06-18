import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App.jsx'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{ algorithm: theme.defaultAlgorithm, token: { colorPrimary: '#1677ff' } }}
    >
      <App />
    </ConfigProvider>
  </React.StrictMode>,
)
