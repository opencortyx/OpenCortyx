# OpenCortyx installer for Windows.
#   irm https://raw.githubusercontent.com/opencortyx/OpenCortyx/main/install.ps1 | iex
$ErrorActionPreference = "Stop"

$Repo = if ($env:OPENCORTYX_REPO) { $env:OPENCORTYX_REPO } else { "https://github.com/opencortyx/OpenCortyx.git" }
$Branch = $env:OPENCORTYX_BRANCH
$Dir = if ($env:OPENCORTYX_HOME) { $env:OPENCORTYX_HOME } else { Join-Path $HOME "opencortyx" }

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Die($msg) { Write-Host "x  $msg" -ForegroundColor Red; exit 1 }
function Invoke-Cli($dir, $command) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dir "bin\opencortyx.ps1") $command
    if ($LASTEXITCODE -ne 0) { Die "opencortyx $command failed" }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Die "Install Docker Desktop first: https://docs.docker.com/desktop/setup/install/windows-install/" }
& docker info *> $null
if ($LASTEXITCODE -ne 0) { Die "Docker is not running. Start Docker Desktop and re-run the installer." }
& docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Die "Docker Compose v2 is required." }

$here = $PSScriptRoot
if ($here -and (Test-Path (Join-Path $here "compose.yaml")) -and (Test-Path (Join-Path $here "bin\opencortyx.ps1"))) {
    $Dir = $here
    Info "Using checkout at $Dir"
    Invoke-Cli $Dir "init"
    Invoke-Cli $Dir "up"
} elseif (Test-Path (Join-Path $Dir ".git")) {
    Info "Updating $Dir"
    Invoke-Cli $Dir "init"
    Invoke-Cli $Dir "update"
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "git is required: https://git-scm.com/download/win" }
    if (-not $Branch) {
        $tags = & git ls-remote --tags --refs $Repo "v*" 2>$null | ForEach-Object { ($_ -split "/")[-1] }
        $Branch = $tags | Where-Object { $_ -match '^v\d+\.\d+\.\d+$' } |
            Sort-Object { [version]($_.TrimStart("v")) } -Descending | Select-Object -First 1
        if (-not $Branch) { $Branch = "main" }
    }
    Info "Downloading OpenCortyx $Branch to $Dir"
    & git clone --depth 1 --branch $Branch $Repo $Dir
    if ($LASTEXITCODE -ne 0) { Die "git clone failed" }
    Invoke-Cli $Dir "init"
    Invoke-Cli $Dir "up"
}

$binDir = Join-Path $Dir "bin"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not ($userPath -split ";" | Where-Object { $_ -eq $binDir })) {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$binDir", "User")
    Info "Added $binDir to your PATH (open a new terminal to use 'opencortyx')"
}
