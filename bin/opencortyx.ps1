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

# Runs a native command whose stderr is noise or an expected failure (an image not pulled yet,
# Docker stopped) and returns its stdout; callers read $LASTEXITCODE. Windows PowerShell 5.1 turns
# redirected native stderr into a terminating error under $ErrorActionPreference = "Stop", so a plain
# `& docker image inspect <not-yet-pulled> *> $null` aborted a first install.
function Invoke-Quiet {
    $ErrorActionPreference = "Continue"
    $exe = $args[0]
    $rest = @($args | Select-Object -Skip 1)
    & $exe @rest 2>$null
}

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
    Invoke-Quiet docker info | Out-Null
    if ($LASTEXITCODE -ne 0) { Die "Docker is not running. Start Docker Desktop and try again." }
    Invoke-Quiet docker compose version | Out-Null
    if ($LASTEXITCODE -ne 0) { Die "Docker Compose v2 is required." }
}

# Waits on the service's own Docker health check rather than polling its port: a script making web
# requests in a loop is what behaviour-based antivirus flags (Avast reported this CLI as IDP.Generic
# while it polled the API), and the health check already asks the right question from inside.
function Wait-Healthy($service, $name, $tries) {
    for ($i = 0; $i -lt $tries; $i++) {
        $id = Invoke-Compose ps -q $service | Select-Object -First 1
        if ($id) {
            $state = Invoke-Quiet docker inspect -f "{{.State.Health.Status}}" $id
            if ($state -eq "healthy") { return $true }
        }
        Start-Sleep -Seconds 5
    }
    Warn "$name did not become healthy"
    return $false
}

function Port($key, $default) { $v = Get-EnvValue $key; if ($v) { $v } else { $default } }

function Invoke-Init {
    $fresh = $false
    if (-not (Test-Path $EnvFile)) { Copy-Item (Join-Path $Root ".env.example") $EnvFile; Info "Created .env"; $fresh = $true }
    foreach ($key in @("POSTGRES_PASSWORD", "JWT_SECRET", "NEXTAUTH_SECRET")) {
        if (-not (Get-EnvValue $key)) { Set-EnvValue $key (New-Secret); Info "Generated $key" }
    }
    # The release channel `update` follows; the installer passes the one it installed from.
    if ($env:OPENCORTYX_CHANNEL) { Set-EnvValue "OPENCORTYX_CHANNEL" $env:OPENCORTYX_CHANNEL }
    # Which models run is chosen on the Models page (the Model Registry), never in .env.
    if ($fresh) {
        Info "After sign-in, open Models and connect a model server (Ollama, LM Studio, a hosted API, ...)"
        Info "by its address - see docs/models.md."
    }
    New-Item -ItemType Directory -Force (Join-Path $Root "data\files") | Out-Null
}

function Invoke-Up {
    Assert-Docker
    if (-not (Test-Path $EnvFile)) { Invoke-Init }
    $version = $env:OPENCORTYX_VERSION
    $apiUrl = Get-EnvValue "PUBLIC_API_URL"
    $build = ($env:OPENCORTYX_BUILD -eq "1") -or ($env:OPENCORTYX_DEV -eq "1") -or ($version -like "*dev*") -or ($apiUrl -and $apiUrl -ne "http://localhost:8000")
    if ($build) {
        Info "Building OpenCortyx $version from source (first run takes several minutes)"
        Invoke-Compose up -d --build
    } else {
        Info "Starting OpenCortyx $version (pulling release images; building any that are unavailable)"
        Invoke-Quiet docker compose --project-directory $Root -f compose.yaml pull --ignore-pull-failures --quiet | Out-Null
        Invoke-Compose up -d
    }
    $api = "http://localhost:$(Port 'OPENCORTYX_API_PORT' '8000')"
    $web = Get-EnvValue "PUBLIC_WEB_URL"; if (-not $web) { $web = "http://localhost:3000" }
    Info "Waiting for the API"
    if (-not (Wait-Healthy "api" "API" 180)) { Invoke-Compose ps; Die "API is not healthy. See: bin\opencortyx logs api" }
    Info "Waiting for the dashboard"
    Wait-Healthy "web" "Dashboard" 60 | Out-Null
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
    Invoke-Quiet docker info | Out-Null
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

# Newest of the given tags. versionsort.suffix ranks v1.0.0-beta.2 below v1.0.0; git's plain
# version sort puts it above, which would move a stable install back onto a beta.
function Get-NewestTag {
    & git -c versionsort.suffix=- tag -l @args --sort=-v:refname | Select-Object -First 1
}

# Newest release on a channel: stable is vX.Y.Z only, beta also takes vX.Y.Z-beta.N.
function Get-ReleaseTag($channel) {
    $pattern = if ($channel -eq "beta") { '^v\d+\.\d+\.\d+(-beta\.\d+)?$' } else { '^v\d+\.\d+\.\d+$' }
    & git -c versionsort.suffix=- tag -l "v*" --sort=-v:refname | Where-Object { $_ -match $pattern } | Select-Object -First 1
}

function Invoke-Update($argv) {
    $channel = $env:OPENCORTYX_CHANNEL
    if (-not $channel) { $channel = Get-EnvValue "OPENCORTYX_CHANNEL" }
    if (-not $channel) { $channel = "stable" }
    if ($argv.Count -gt 0) {
        if ($argv[0] -eq "--channel" -and $argv.Count -gt 1) { $channel = $argv[1] }
        elseif ($argv[0] -like "--channel=*") { $channel = $argv[0].Substring(10) }
        else { Die "Usage: bin\opencortyx update [--channel stable|beta]" }
    }
    if ($channel -notin @("stable", "beta")) { Die "Unknown channel '$channel': use stable or beta" }
    & git fetch --tags --quiet
    if ($LASTEXITCODE -ne 0) { Die "git fetch failed" }
    Invoke-Quiet git symbolic-ref -q HEAD | Out-Null
    if ($LASTEXITCODE -eq 0) {
        # A branch checkout (a clone of main) follows its branch, not a release channel.
        & git pull --ff-only
        if ($LASTEXITCODE -ne 0) { Die "git pull failed" }
    } else {
        $latest = Get-ReleaseTag $channel
        $current = "v$(Read-Version)"
        if (-not $latest) {
            Warn "No $channel release yet; staying on $current (beta releases: bin\opencortyx update --channel beta)"
        } elseif ($latest -ne $current -and (Get-NewestTag $latest $current) -eq $current) {
            Warn "$current is newer than the latest $channel release ($latest); staying on it"
        } else {
            & git checkout --quiet $latest
            if ($LASTEXITCODE -ne 0) { Die "git checkout $latest failed" }
            Info "Checked out $latest ($channel channel)"
        }
    }
    if (Test-Path $EnvFile) { Set-EnvValue "OPENCORTYX_CHANNEL" $channel }
    # Start the new version with ITS OWN script. This process is still running the code of the release
    # it was started from, so calling Invoke-Up here ran the old release's `up` against the new files:
    # 0.1.0-beta.2's started a GPU model server that 0.1.0-beta.3 no longer has. `init` first adds any
    # setting the new version introduced; it never changes one that is already set.
    Remove-Item Env:OPENCORTYX_VERSION -ErrorAction SilentlyContinue
    $shell = (Get-Process -Id $PID).Path            # powershell.exe or pwsh.exe, whichever runs this
    $script = Join-Path $Root "bin\opencortyx.ps1"
    & $shell -NoProfile -ExecutionPolicy RemoteSigned -File $script init
    if ($LASTEXITCODE -ne 0) { Die "opencortyx init failed" }
    & $shell -NoProfile -ExecutionPolicy RemoteSigned -File $script up
    exit $LASTEXITCODE
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

  init                    Create .env and generate its secrets
  up [--build]            Start the stack (release images, or build from source), wait until healthy
  dev                     Start with live source mounts and the Next.js dev server
  down                    Stop the stack (data is kept)
  restart [service]       Restart the stack or one service
  status                  Show service status
  logs [service]          Follow logs
  doctor                  Check host, services, database, LLM and embeddings
  connect <client> <key>  Connect an MCP client (claude, cursor, vscode, json)
  reset-password <email>  Set a new password for an account (asks for it; for a locked-out admin)
  update [--channel stable|beta]
                          Move to the newest release on the channel (default stable, remembered) and restart
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
    "update" { Invoke-Update $Rest }
    "reset-password" { Assert-Docker; Invoke-Compose exec api python -m opencortyx.reset_password @Rest }
    "backup" { Invoke-Backup ($Rest | Select-Object -First 1) }
    "restore" { Invoke-Restore ($Rest | Select-Object -First 1) }
    "reset" { Invoke-Reset }
    "test" { Assert-Docker; Invoke-Compose run --rm --no-deps -T api python -m pytest -q -p no:cacheprovider @Rest }
    { $_ -in "help", "-h", "--help" } { Show-Usage }
    default { Show-Usage; exit 1 }
}
