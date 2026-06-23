param(
    [string]$ComposeFile = "infra/docker-compose/compose.yml",
    [string]$BaseUrl = "http://127.0.0.1:5173",
    [string]$InternalToken = "change-me",
    [int]$TimeoutSeconds = 180,
    [switch]$NoStart,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    if (-not $Json) {
        Write-Host "==> $Message"
    }
}

function Invoke-SmokeRequest {
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

function Wait-ComposeServicesHealthy {
    param(
        [string]$ComposeFile,
        [int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $raw = & docker compose -f $ComposeFile ps --format json
        if ($LASTEXITCODE -ne 0) {
            throw "docker compose ps failed"
        }
        $services = @()
        foreach ($line in @($raw)) {
            if ([string]::IsNullOrWhiteSpace($line)) {
                continue
            }
            $services += ($line | ConvertFrom-Json)
        }
        $targets = $services | Where-Object {
            $_.Service -in @(
                "web-portal",
                "knowledge-api",
                "ingestion-worker",
                "rca-agent",
                "agent-platform-api",
                "tool-registry",
                "approval-service",
                "api-gateway"
            )
        }
        $unhealthy = $targets | Where-Object {
            $_.State -ne "running" -or ($_.Health -and $_.Health -ne "healthy")
        }
        if ($targets.Count -ge 8 -and $unhealthy.Count -eq 0) {
            return $targets
        }
        Start-Sleep -Seconds 3
    }

    & docker compose -f $ComposeFile ps
    throw "Timed out waiting for Docker Compose services to become healthy"
}

function Assert-HttpOk {
    param([string]$Name, [string]$Url)
    $res = Invoke-SmokeRequest -Url $Url
    if ($res.StatusCode -lt 200 -or $res.StatusCode -ge 300) {
        throw "$Name returned HTTP $($res.StatusCode)"
    }
    return @{
        name = $Name
        status = $res.StatusCode
        sample = $res.Content.Substring(0, [Math]::Min(160, $res.Content.Length))
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot
$runStamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

if (-not $NoStart) {
    Write-Step "Starting Docker Compose stack"
    & docker compose -f $ComposeFile up -d --build web-portal api-gateway
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose up failed"
    }
}

Write-Step "Waiting for healthy services"
$services = Wait-ComposeServicesHealthy -ComposeFile $ComposeFile -TimeoutSeconds $TimeoutSeconds

Write-Step "Checking HTTP endpoints"
$httpChecks = @()
$httpChecks += Assert-HttpOk "web" "http://127.0.0.1:5173/"
$httpChecks += Assert-HttpOk "knowledge" "$BaseUrl/api/knowledge/health"
$httpChecks += Assert-HttpOk "rca" "$BaseUrl/api/rca/health"
$httpChecks += Assert-HttpOk "platform" "$BaseUrl/api/platform/health"
$httpChecks += Assert-HttpOk "tools" "$BaseUrl/api/tools/health"
$httpChecks += Assert-HttpOk "platform-timeseries" "$BaseUrl/api/platform/api/v1/metrics/platform/timeseries"

Write-Step "Uploading and publishing RAG demo document"
$docPath = Join-Path $repoRoot "Docs/project-1-rag-knowledge-base-design-spec.md"
$title = "docker-smoke-rag-$runStamp"
$uploadRaw = & curl.exe -s -X POST "$BaseUrl/api/knowledge/api/v1/documents" `
    -H "X-Internal-Token: $InternalToken" `
    -F "file=@$docPath;type=text/markdown" `
    -F "title=$title" `
    -F "mime_type=text/markdown"
if ($LASTEXITCODE -ne 0) {
    throw "RAG document upload failed"
}
$document = $uploadRaw | ConvertFrom-Json
if (-not $document.doc_id) {
    throw "RAG document upload returned no doc_id: $uploadRaw"
}

$publish = Invoke-SmokeRequest `
    -Method "POST" `
    -Url "$BaseUrl/api/knowledge/api/v1/documents/$($document.doc_id)/publish" `
    -Headers @{ "X-Internal-Token" = $InternalToken }
$published = $publish.Content | ConvertFrom-Json
if ($published.parse_status -ne "published") {
    throw "RAG document was not published: $($publish.Content)"
}

Write-Step "Running RAG question"
$question = "某 5G 小区出现 RRC 建立失败率升高，应该先查什么？"
$ragResponse = Invoke-SmokeRequest `
    -Method "POST" `
    -Url "$BaseUrl/api/knowledge/api/v1/chat/query" `
    -Body @{
        session_id = "docker-smoke-$runStamp"
        question = $question
        knowledge_scopes = @()
    }
$rag = $ragResponse.Content | ConvertFrom-Json
if (-not $rag.answer -or $rag.citations.Count -lt 1) {
    throw "RAG query did not return answer with citations: $($ragResponse.Content)"
}

Write-Step "Creating RCA run from replay sample"
$samplePath = Join-Path $repoRoot "tests/rca-replay/sample_cases.jsonl"
$case = (Get-Content $samplePath | Select-Object -First 1) | ConvertFrom-Json
$rcaResponse = Invoke-SmokeRequest `
    -Method "POST" `
    -Url "$BaseUrl/api/rca/api/v1/rca/runs" `
    -Headers @{ "X-Internal-Token" = $InternalToken } `
    -Body @{
        alarms = $case.alarms
        require_human_review = $true
    }
$rca = $rcaResponse.Content | ConvertFrom-Json
if (-not $rca.run_id -or $rca.evidence_count -lt 1 -or $rca.hypotheses.Count -lt 1) {
    throw "RCA run did not return evidence and hypotheses: $($rcaResponse.Content)"
}

$summary = @{
    ok = $true
    base_url = $BaseUrl
    services = $services | ForEach-Object {
        @{ service = $_.Service; state = $_.State; health = $_.Health }
    }
    http_checks = $httpChecks
    rag = @{
        doc_id = $document.doc_id
        parse_status = $published.parse_status
        citation_count = $rag.citations.Count
        answer_preview = $rag.answer.Substring(0, [Math]::Min(160, $rag.answer.Length))
    }
    rca = @{
        run_id = $rca.run_id
        incident_id = $rca.incident_id
        report_id = $rca.report_id
        status = $rca.status
        evidence_count = $rca.evidence_count
        hypothesis_count = $rca.hypotheses.Count
    }
}

if ($Json) {
    $summary | ConvertTo-Json -Depth 20
} else {
    $summary | ConvertTo-Json -Depth 20
    Write-Host "Docker smoke completed for $BaseUrl"
}
