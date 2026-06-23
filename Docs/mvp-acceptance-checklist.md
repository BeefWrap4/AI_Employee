# MVP 端到端验收清单

本文档用于验收本地 Docker Compose 下的可运行 MVP。验收前确认工作区已安装 Docker、Node.js/npm、PowerShell，并在仓库根目录执行命令。

## 1. Docker 栈启动与健康检查

- 运行 `powershell -ExecutionPolicy Bypass -File scripts\docker-smoke.ps1 -Json`。
- 验证输出 `ok` 为 `true`。
- 确认 HTTP 健康检查覆盖 `web-portal`、`knowledge-api`、`rca-agent`、`agent-platform-api`、`tool-registry`。
- 失败时先查看 `docker compose -f infra\docker-compose\compose.yml ps -a` 和对应服务日志。

## 2. 演示数据准备

- 运行 `powershell -ExecutionPolicy Bypass -File scripts\seed-demo.ps1 -Json`。
- 确认输出包含 RAG 文档、RCA run、tool-registry 工具、platform tool、Agent run。
- RAG 文档应为 BS-310042 小区案例，包含 RRC 建立失败率、PRB 利用率、驻波比告警、回传链路误码和变更窗口。

## 3. 前端演示流程与 Agent 平台运行记录

- 打开 `http://127.0.0.1:5173`。
- 在“平台总览”确认“演示流程”存在，并可跳转到知识问答、RCA 诊断、运行实况。
- 在“知识库”输入 `BS-310042 RRC 建立失败率升高先查什么？`，应显示“问答结果”和“引用证据”。
- 在“RCA 诊断”打开最新 run，报告应展示 Top-N 根因候选和证据链。
- 在“运行实况”查看最近运行记录，打开详情后应显示输入/输出、节点轨迹、工具调用和审批状态。

## 4. API 健康检查

- Knowledge：`GET /api/knowledge/health`
- RCA：`GET /api/rca/health`
- Platform：`GET /api/platform/health`
- Tools：`GET /api/tools/health`
- Platform metrics：`GET /api/platform/api/v1/metrics/platform/timeseries`

所有接口应返回 2xx；健康检查失败即不通过验收。

## 5. 自动化验证

```powershell
pytest tests\test_docker_smoke_script.py tests\test_demo_seed_script.py tests\test_web_e2e_contract.py tests\test_readme_acceptance_docs.py -q
cd apps\web-portal
npm run build
npm run e2e:docker
```

已知非阻塞项：Vite 可能提示 chunk size 较大，ECharts 在 jsdom 单元测试中可能提示容器尺寸为 0；只要命令退出码为 0，即可继续验收。
