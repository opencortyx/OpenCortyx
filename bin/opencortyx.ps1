# OpenCortyx command-line tool (Windows PowerShell 5.1+ / PowerShell 7).
param(
    [Parameter(Position = 0)][string]$Command = "help",
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest = @()
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$EnvFile = Join-Path $Root ".env"

function Read-Version {
    $file = Join-Path $Root "VERSION"
    if (Test-Path $file) { return (Get-Content $file -Raw).Trim() } else { return "dev" }
}
if (-not $env:OPENCORTYX_VERSION) { $env:OPENCORTYX_VERSION = Read-Version }

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "!  $msg" -ForegroundColor Yellow }
function Die($msg) { Write-Host "x  $msg" -ForegroundColor Red; exit 1 }

function Invoke-Compose {
    $files = @("-f", "compose.yaml")
    if ($env:OPENCORTYX_DEV -eq "1") { $files += @("-f", "compose.dev.yaml") }
    & docker compose --project-directory $Root @files @args
    if ($LASTEXITCODE -ne 0) { throw "docker compose $($args -join ' ') failed ($LASTEXITCODE)" }
}

function Get-EnvValue($key) {
    if (-not (Test-Path $EnvFile)) { return "" }
    $line = Get-Content $EnvFile | Where-Object { $_ -match "^$key=" } | Select-Object -Last 1
    if ($line) { return $line.Substring($key.Length + 1).TrimEnd("`r") } else { return "" }
}

function Set-EnvValue($key, $value) {
    $lines = @(Get-Content $EnvFile)
    $found = $false
    $out = foreach ($l in $lines) {
        if ($l -match "^$key=") { $found = $true; "$key=$value" } else { $l }
    }
    if (-not $found) { $out = @($out) + "$key=$value" }
    $text = (([string[]]$out) -join "`n") + "`n"
    [System.IO.File]::WriteAllText($EnvFile, $text, (New-Object System.Text.UTF8Encoding $false))
}

function New-Secret {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return -join ($bytes | ForEach-Object { $_.ToString("x2") })
}

function Assert-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Die "Docker is not installed: https://docs.docker.com/desktop/setup/install/windows-install/" }
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) { Die "Docker is not running. Start Docker Desktop and try again." }
    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) { Die "Docker Compose v2 is required." }
}

function Test-NvidiaGpu {
    $runtimes = & docker info --format "{{json .Runtimes}}" 2>$null
    if ($runtimes -match "nvidia") { return $true }
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        & nvidia-smi -L *> $null
        return ($LASTEXITCODE -eq 0)
    }
    return $false
}

# Fraction of GPU memory vLLM may reserve: what is free now, minus a margin, capped at 0.90.
# Measured the way vLLM checks it (torch, inside its image); nvidia-smi overstates usable memory.
function Get-GpuFraction {
    $line = $null
    $margin = 768
    $tag = Get-EnvValue "VLLM_IMAGE_TAG"; if (-not $tag) { $tag = "v0.11.0" }
    $image = "vllm/vllm-openai:$tag"
    & docker image inspect $image *> $null
    if ($LASTEXITCODE -eq 0) {
        $probe = 'import torch; f, t = torch.cuda.mem_get_info(); print(f"{t // 2**20}, {f // 2**20}")'
        $line = & docker run --rm --gpus all --entrypoint python3 $image -c $probe 2>$null | Select-Object -Last 1
        if ($line -match '^\s*\d+,\s*\d+\s*$') { $margin = 256 } else { $line = $null }
    }
    if (-not $line -and (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
        $line = & nvidia-smi --query-gpu=memory.total,memory.free --format=csv,noheader,nounits 2>$null | Select-Object -First 1
    }
    if (-not $line) { return $null }
    $parts = $line -split ","
    $total = [double]$parts[0].Trim()
    $free = [double]$parts[1].Trim()
    if ($total -le 0) { return $null }
    $x = [math]::Min(0.90, [math]::Max(0.50, ($free - $margin) / $total))
    return ([math]::Floor($x * 100) / 100).ToString("0.00", [Globalization.CultureInfo]::InvariantCulture)
}

function Set-GpuFraction {
    if (("," + (Get-EnvValue "COMPOSE_PROFILES") + ",") -notmatch ",gpu,") { return }
    if ((Get-EnvValue "OPENCORTYX_GPU_FRACTION_FIXED") -eq "1") { return }
    $running = & docker compose --project-directory $Root -f compose.yaml ps --status running -q vllm 2>$null
    if ($running) { return }
    $frac = Get-GpuFraction
    if (-not $frac) { return }
    Set-EnvValue "VLLM_GPU_FRACTION" $frac
    Info "vLLM GPU memory fraction: $frac"
}

function Wait-Http($url, $name, $tries) {
    for ($i = 0; $i -lt $tries; $i++) {
        try {
            $r = Invoke-WebRequest -Uri $url -TimeoutSec 5 -UseBasicParsing -MaximumRedirection 0 -ErrorAction Stop
            if ($r.StatusCode -lt 400) { return $true }
        } catch {
            $resp = $_.Exception.Response
            if ($resp -and [int]$resp.StatusCode -lt 400) { return $true }
        }
        Start-Sleep -Seconds 5
    }
    Warn "$name did not become ready at $url"
    return $false
}

function Port($key, $default) { $v = Get-EnvValue $key; if ($v) { $v } else { $default } }

function Invoke-Init {
    $fresh = $false
    if (-not (Test-Path $EnvFile)) { Copy-Item (Join-Path $Root ".env.example") $EnvFile; Info "Created .env"; $fresh = $true }
    foreach ($key in @("POSTGRES_PASSWORD", "JWT_SECRET", "NEXTAUTH_SECRET")) {
        if (-not (Get-EnvValue $key)) { Set-EnvValue $key (New-Secret); Info "Generated $key" }
    }
    # Which models run is chosen on the Models page, not in .env. The only install-time decision
    # is whether to start the local vLLM server, and only for a new .env.
    if ($fresh) {
        if (Test-NvidiaGpu) {
            Set-EnvValue "COMPOSE_PROFILES" "gpu"
            Info "NVIDIA GPU detected: starting the local vLLM model server"
        } else {
            Warn "No NVIDIA GPU: after sign-in, open Models and point generation at your model server"
            Warn "(Ollama, LM Studio, OpenAI, ...) - see docs/models.md."
        }
    }
    New-Item -ItemType Directory -Force (Join-Path $Root "data\files") | Out-Null
}

function Invoke-Up {
    Assert-Docker
    if (-not (Test-Path $EnvFile)) { Invoke-Init }
    Set-GpuFraction
    $version = $env:OPENCORTYX_VERSION
    $apiUrl = Get-EnvValue "PUBLIC_API_URL"
    $build = ($env:OPENCORTYX_BUILD -eq "1") -or ($env:OPENCORTYX_DEV -eq "1") -or ($version -like "*dev*") -or ($apiUrl -and $apiUrl -ne "http://localhost:8000")
    if ($build) {
        Info "Building OpenCortyx $version from source (first run takes several minutes)"
        Invoke-Compose up -d --build
    } else {
        Info "Starting OpenCortyx $version (pulling release images; building any that are unavailable)"
        & docker compose --project-directory $Root -f compose.yaml pull --ignore-pull-failures --quiet *> $null
        Invoke-Compose up -d
    }
    $api = "http://localhost:$(Port 'OPENCORTYX_API_PORT' '8000')"
    $web = Get-EnvValue "PUBLIC_WEB_URL"; if (-not $web) { $web = "http://localhost:3000" }
    Info "Waiting for the API"
    if (-not (Wait-Http "$api/health" "API" 180)) { Invoke-Compose ps; Die "API is not healthy. See: bin\opencortyx logs api" }
    Info "Waiting for the dashboard"
    Wait-Http "$web/auth/login" "Dashboard" 60 | Out-Null
    Write-Host ""
    Write-Host "OpenCortyx is running" -ForegroundColor Green
    Write-Host "  Dashboard  $web"
    Write-Host "  API        $api/docs"
    Write-Host "  MCP        http://localhost:$(Port 'OPENCORTYX_MCP_PORT' '8001')/mcp"
    Write-Host "  Next       bin\opencortyx doctor"
}

function Invoke-Doctor {
    $failed = $false
    Write-Host "Host" -ForegroundColor White
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) { Write-Host "  x docker is not running"; exit 1 }
    Write-Host "  ok docker $(& docker version --format '{{.Server.Version}}')"
    Write-Host "  ok $(& docker compose version | Select-Object -First 1)"
    $mem = [int64](& docker info --format "{{.MemTotal}}")
    if ($mem -lt 7000000000) { Write-Host "  !  docker has $([math]::Round($mem / 1MB)) MiB memory; 8 GiB+ recommended" }
    else { Write-Host "  ok docker memory $([math]::Round($mem / 1MB)) MiB" }
    if (Test-Path $EnvFile) {
        foreach ($key in @("POSTGRES_PASSWORD", "JWT_SECRET", "NEXTAUTH_SECRET")) {
            if (-not (Get-EnvValue $key)) { Write-Host "  x $key is empty in .env"; $failed = $true }
        }
        if (-not $failed) { Write-Host "  ok .env secrets set" }
    } else { Write-Host "  x .env missing (run bin\opencortyx init)"; $failed = $true }
    if (Test-NvidiaGpu) { Write-Host "  ok NVIDIA GPU available" } else { Write-Host "  -  no NVIDIA GPU" }
    Write-Host "Services" -ForegroundColor White
    Invoke-Compose ps --format "  {{.Service}}: {{.Status}}"
    Write-Host "Runtime" -ForegroundColor White
    # A separate container, so loading the embedding model cannot push the running API over its limit.
    & docker compose --project-directory $Root -f compose.yaml run --rm --no-deps -T api python -m opencortyx.doctor
    if ($LASTEXITCODE -ne 0) { $failed = $true }
    if ($failed) { exit 1 }
}

function Invoke-Connect($client, $key) {
    if (-not $client) { Die "usage: bin\opencortyx connect <claude|cursor|vscode|json> <api-key>" }
    if (-not $key) { Die "Create an API key in the dashboard (Users & Access), then pass it here." }
    $url = "http://localhost:$(Port 'OPENCORTYX_MCP_PORT' '8001')/mcp"
    $json = @"
{
  "mcpServers": {
    "opencortyx": {
      "type": "http",
      "url": "$url",
      "headers": { "X-API-Key": "$key" }
    }
  }
}
"@
    switch ($client) {
        "claude" {
            if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { Write-Host $json; Die "Claude Code CLI not found; add the config above manually." }
            & claude mcp add --transport http opencortyx $url --header "X-API-Key: $key"
            Info "Registered 'opencortyx' with Claude Code"
        }
        "cursor" { Write-Host $json; Info "Add this to $HOME\.cursor\mcp.json" }
        "vscode" { Write-Host $json; Info "Add this to .vscode\mcp.json (use `"servers`" instead of `"mcpServers`")" }
        "json" { Write-Host $json }
        default { Die "unknown client: $client" }
    }
}

# The three stores keep their state in Docker volumes, so one backup covers all of them: the control
# plane (PostgreSQL), the graph (NebulaGraph) and the search surfaces (Qdrant). The stack is stopped
# for the copy, because a database's files are only consistent when nothing is writing to them.
$StoreVolumes = @("postgres_data", "qdrant_data", "nebula_meta_data", "nebula_storage_data")

function Get-ProjectName {
    $name = Get-EnvValue "COMPOSE_PROJECT_NAME"
    if (-not $name) { $name = "opencortyx" }
    return $name
}

function Copy-StoreVolume($volume, $direction, $file) {
    $project = Get-ProjectName
    $folder = (Resolve-Path (Split-Path -Parent $file)).Path
    $name = Split-Path -Leaf $file
    if ($direction -eq "save") {
        & docker run --rm -v "${project}_${volume}:/store:ro" -v "${folder}:/backup" `
            alpine:3.20 tar czf "/backup/$name" -C /store . | Out-Null
    } else {
        & docker run --rm -v "${project}_${volume}:/store" -v "${folder}:/backup:ro" `
            alpine:3.20 sh -c "rm -rf /store/* /store/..?* /store/.[!.]* 2>/dev/null; tar xzf /backup/$name -C /store" | Out-Null
    }
    if ($LASTEXITCODE -ne 0) { Die "volume $volume ($direction) failed" }
}

function Invoke-Backup($dir) {
    Assert-Docker
    if (-not $dir) { $dir = "backups" }
    $target = Join-Path $dir ("opencortyx_{0}" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
    New-Item -ItemType Directory -Force $target | Out-Null
    Info "Stopping the stack for a consistent copy"
    Invoke-Compose stop | Out-Null
    try {
        foreach ($volume in $StoreVolumes) {
            Info "Archiving $volume"
            Copy-StoreVolume $volume "save" (Join-Path $target "$volume.tar.gz")
        }
        $manifest = @{ version = $env:OPENCORTYX_VERSION; created_at = (Get-Date).ToUniversalTime().ToString("s") + "Z"; volumes = $StoreVolumes }
        $manifest | ConvertTo-Json -Compress | Out-File -Encoding utf8 (Join-Path $target "manifest.json")
    } finally {
        Invoke-Compose start | Out-Null   # the stack comes back even when the copy failed
    }
    $size = [math]::Round(((Get-ChildItem $target -Recurse | Measure-Object Length -Sum).Sum / 1MB), 1)
    Info "Backup written: $target ($size MiB)"
}

function Invoke-Restore($dir) {
    Assert-Docker
    if (-not $dir -or -not (Test-Path (Join-Path $dir "manifest.json"))) {
        Die "usage: bin\opencortyx restore <backup-directory>"
    }
    if ($env:OPENCORTYX_YES -ne "1") {
        $answer = Read-Host "This replaces the database, the graph and the vectors with $dir. Type 'restore' to confirm"
        if ($answer -ne "restore") { Die "Aborted" }
    }
    Invoke-Compose stop | Out-Null
    foreach ($volume in $StoreVolumes) {
        $file = Join-Path $dir "$volume.tar.gz"
        if (-not (Test-Path $file)) { Die "$dir is missing $volume.tar.gz" }
        Info "Restoring $volume"
        Copy-StoreVolume $volume "load" $file
    }
    Invoke-Compose up -d
    Info "Restore complete"
}

function Invoke-Reset {
    Assert-Docker
    if ($env:OPENCORTYX_YES -ne "1") {
        $answer = Read-Host "This deletes ALL OpenCortyx data (database, cache, models). Type 'reset' to confirm"
        if ($answer -ne "reset") { Die "Aborted" }
    }
    Invoke-Compose down -v
    Info "All data removed. Run bin\opencortyx up to start fresh."
}

function Show-Usage {
    @"
OpenCortyx

Usage: bin\opencortyx <command> [args]

  init                    Create .env, generate secrets, pick the LLM backend
  up [--build]            Start the stack (release images, or build from source), wait until healthy
  dev                     Start with live source mounts and the Next.js dev server
  down                    Stop the stack (data is kept)
  restart [service]       Restart the stack or one service
  status                  Show service status
  logs [service]          Follow logs
  doctor                  Check host, services, database, LLM and embeddings
  connect <client> <key>  Connect an MCP client (claude, cursor, vscode, json)
  update                  Pull the latest code and restart
  backup [dir]            Back up the database, the graph and the vectors (stops the stack briefly)
  restore <dir>           Restore a backup directory
  reset                   Delete all data
  test [pytest args]      Run the API test suite in a container
"@ | Write-Host
}

switch ($Command) {
    "init" { Invoke-Init }
    { $_ -in "up", "start" } { if ($Rest -contains "--build") { $env:OPENCORTYX_BUILD = "1" }; Invoke-Up }
    "dev" { $env:OPENCORTYX_DEV = "1"; Invoke-Up }
    "version" { Write-Host $env:OPENCORTYX_VERSION }
    { $_ -in "down", "stop" } { Assert-Docker; Invoke-Compose stop }
    "restart" { Assert-Docker; Invoke-Compose restart @Rest }
    { $_ -in "status", "ps" } { Assert-Docker; Invoke-Compose ps }
    "logs" { Assert-Docker; Invoke-Compose logs -f --tail=200 @Rest }
    "doctor" { Invoke-Doctor }
    "connect" { Invoke-Connect ($Rest | Select-Object -First 1) ($Rest | Select-Object -Skip 1 -First 1) }
    "update" { Invoke-Update }
    "backup" { Invoke-Backup ($Rest | Select-Object -First 1) }
    "restore" { Invoke-Restore ($Rest | Select-Object -First 1) }
    "reset" { Invoke-Reset }
    "test" { Assert-Docker; Invoke-Compose run --rm --no-deps -T api python -m pytest -q -p no:cacheprovider @Rest }
    { $_ -in "help", "-h", "--help" } { Show-Usage }
    default { Show-Usage; exit 1 }
}
