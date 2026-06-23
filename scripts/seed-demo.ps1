param(
    [string]$BaseUrl = "http://127.0.0.1:5173",
    [string]$InternalToken = "change-me",
    [switch]$Json
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    if (-not $Json) {
        Write-Host "==> $Message"
    }
}

function Invoke-DemoRequest {
    param(
        [string]$Method = "GET",
        [string]$Url,
        [object]$Body = $null,
        [hashtable]$Headers = @{}
    )

    $args = @{
        Uri = $Url
        Method = $Method
        Headers = $Headers
        UseBasicParsing = $true
        TimeoutSec = 30
    }
    if ($null -ne $Body) {
        $args.ContentType = "application/json"
        $args.Body = ($Body | ConvertTo-Json -Depth 20)
    }
    return Invoke-WebRequest @args
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot
$stamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$headers = @{ "X-Internal-Token" = $InternalToken }

Write-Step "Seeding RAG demo document"
$demoDir = Join-Path $repoRoot "var/demo"
New-Item -ItemType Directory -Force -Path $demoDir | Out-Null
$docPath = Join-Path $demoDir "telecom-bs-310042-rrc-failure-runbook.md"
$demoDocContent = @(
    "# BS-310042 5G 小区 RRC 建立失败率升高排障案例",
    "",
    "## 场景摘要",
    "",
    "2026-06-23 09:10，华东一区 gNB-31 的小区 BS-310042 触发 RRC 建立失败率升高告警。近 15 分钟 RRC 建立失败率从 0.8% 上升到 12.6%，同时 PRB 利用率维持在 86% 到 91%，上行干扰均值从 -112 dBm 抬升到 -101 dBm。",
    "",
    "## 关联告警",
    "",
    "- 驻波比告警：AAU-3 端口 2 VSWR 达到 2.1，持续 8 分钟。",
    "- 回传链路误码：S1-U 方向出现 CRC error burst，峰值 320 errors/min。",
    "- 邻区切换失败：NCI 460-11-310043 的 Xn handover failure rate 达到 7.4%。",
    "- 配置变更窗口：08:45 到 09:00 执行过 PCI/功率参数批量变更。",
    "",
    "## 排查顺序",
    "",
    "1. 先确认告警时间线和变更窗口是否重叠，优先回看功率、PCI、邻区关系和切换参数。",
    "2. 检查 PRB 利用率、RRC 建立失败率、ERAB 掉线率、上行干扰和 PUSCH BLER 是否同步异常。",
    "3. 若驻波比告警仍在持续，安排现场或远程射频链路检查，关注馈线、AAU 端口和天线驻波。",
    "4. 若回传链路误码持续，联动传输专业检查光模块、链路误码和交换机端口丢包。",
    "5. 输出 RCA 报告时，需要保留告警、KPI、拓扑邻区、变更记录和工具调用证据。",
    "",
    "## 根因候选",
    "",
    "- 候选 A：射频链路异常导致接入失败，证据为驻波比告警和上行干扰同步升高。",
    "- 候选 B：变更窗口参数异常导致接入和切换失败，证据为故障开始时间紧邻参数变更。",
    "- 候选 C：回传链路误码导致控制面/用户面质量下降，证据为 S1-U CRC error burst。",
    "",
    "## 处置建议",
    "",
    "先冻结同批次参数变更，回滚 BS-310042 的功率和邻区参数到变更前版本；同时派单检查 AAU-3 端口 2 驻波比。若回滚后 RRC 建立失败率 10 分钟内未下降，再升级传输链路误码排查。"
) -join [Environment]::NewLine
Set-Content -Path $docPath -Value $demoDocContent -Encoding UTF8
$uploadRaw = & curl.exe -s -X POST "$BaseUrl/api/knowledge/api/v1/documents" `
    -H "X-Internal-Token: $InternalToken" `
    -F "file=@$docPath;type=text/markdown" `
    -F "title=BS-310042 RRC failure runbook" `
    -F "mime_type=text/markdown"
if ($LASTEXITCODE -ne 0) {
    throw "RAG document upload failed"
}
$document = $uploadRaw | ConvertFrom-Json
$publish = Invoke-DemoRequest `
    -Method "POST" `
    -Url "$BaseUrl/api/knowledge/api/v1/documents/$($document.doc_id)/publish" `
    -Headers $headers
$published = $publish.Content | ConvertFrom-Json

Write-Step "Seeding RAG demo query"
$ragQuestion = "某 5G 小区出现 RRC 建立失败率升高，应该先查什么？"
$ragResponse = Invoke-DemoRequest `
    -Method "POST" `
    -Url "$BaseUrl/api/knowledge/api/v1/chat/query" `
    -Body @{
        session_id = "demo-seed-$stamp"
        question = $ragQuestion
        knowledge_scopes = @()
    }
$rag = $ragResponse.Content | ConvertFrom-Json

Write-Step "Seeding RCA demo run"
$samplePath = Join-Path $repoRoot "tests/rca-replay/sample_cases.jsonl"
$case = (Get-Content $samplePath | Select-Object -First 1) | ConvertFrom-Json
$rcaResponse = Invoke-DemoRequest `
    -Method "POST" `
    -Url "$BaseUrl/api/rca/api/v1/rca/runs" `
    -Headers $headers `
    -Body @{
        alarms = $case.alarms
        require_human_review = $true
    }
$rca = $rcaResponse.Content | ConvertFrom-Json

Write-Step "Seeding tool-registry demo tool"
$toolRegistryResponse = Invoke-DemoRequest `
    -Method "POST" `
    -Url "$BaseUrl/api/tools/api/v1/tools" `
    -Headers $headers `
    -Body @{
        name = "demo.echo"
        description = "Demo read-only echo tool for portal smoke data"
        service_name = "demo"
        version = "v1"
        risk_level = "read_only"
        input_schema = @{ type = "object"; properties = @{ text = @{ type = "string" } } }
        output_schema = @{ type = "object"; properties = @{ echo = @{ type = "string" } } }
        timeout_ms = 5000
        retry_policy = @{ max_retries = 0 }
    }
$toolRegistry = $toolRegistryResponse.Content | ConvertFrom-Json

Write-Step "Seeding platform tool and agent run"
$platformToolName = "demo.platform.echo.$stamp"
$platformToolResponse = Invoke-DemoRequest `
    -Method "POST" `
    -Url "$BaseUrl/api/platform/api/v1/tools" `
    -Headers $headers `
    -Body @{
        tool_name = $platformToolName
        service_name = "demo"
        description = "Demo platform echo tool for run trace examples"
        risk_level = "read_only"
        status = "active"
        input_schema = @{ type = "object"; properties = @{ text = @{ type = "string" } } }
        output_schema = @{ type = "object"; properties = @{ echo = @{ type = "string" } } }
        timeout_ms = 5000
        retry_policy = @{ max_attempts = 1; backoff_seconds = 0 }
        circuit_breaker = @{ failure_threshold = 5; cooldown_seconds = 60 }
    }
$platformTool = $platformToolResponse.Content | ConvertFrom-Json

$agentRunResponse = Invoke-DemoRequest `
    -Method "POST" `
    -Url "$BaseUrl/api/platform/api/v1/agent-runs" `
    -Headers (@{
        "X-Internal-Token" = $InternalToken
        "Idempotency-Key" = "demo-seed-knowledge-qa"
    }) `
    -Body @{
        template_id = "knowledge_qa"
        requested_by = "demo"
        input = @{
            question = $ragQuestion
            source = "seed-demo.ps1"
        }
    }
$agentRun = $agentRunResponse.Content | ConvertFrom-Json

$summary = @{
    ok = $true
    base_url = $BaseUrl
    rag = @{
        doc_id = $document.doc_id
        parse_status = $published.parse_status
        citation_count = $rag.citations.Count
    }
    rca = @{
        run_id = $rca.run_id
        report_id = $rca.report_id
        evidence_count = $rca.evidence_count
        hypothesis_count = $rca.hypotheses.Count
    }
    tool_registry = @{
        name = $toolRegistry.name
        registered = $toolRegistry.registered
    }
    platform = @{
        tool_name = $platformTool.tool_name
        run_id = $agentRun.run_id
        template_id = $agentRun.template_id
        status = $agentRun.status
    }
}

if ($Json) {
    $summary | ConvertTo-Json -Depth 20
} else {
    $summary | ConvertTo-Json -Depth 20
    Write-Host "Demo seed completed for $BaseUrl"
}
