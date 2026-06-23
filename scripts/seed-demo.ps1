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
$docPath = Join-Path $repoRoot "Docs/project-1-rag-knowledge-base-design-spec.md"
$uploadRaw = & curl.exe -s -X POST "$BaseUrl/api/knowledge/api/v1/documents" `
    -H "X-Internal-Token: $InternalToken" `
    -F "file=@$docPath;type=text/markdown" `
    -F "title=Demo RAG knowledge base" `
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
