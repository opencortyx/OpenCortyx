# OpenCortyx installer for Windows.
#   irm https://raw.githubusercontent.com/opencortyx/OpenCortyx/main/install.ps1 | iex
# Beta releases:
#   $env:OPENCORTYX_CHANNEL = "beta"; irm https://raw.githubusercontent.com/opencortyx/OpenCortyx/main/install.ps1 | iex
$ErrorActionPreference = "Stop"

$Repo = if ($env:OPENCORTYX_REPO) { $env:OPENCORTYX_REPO } else { "https://github.com/opencortyx/OpenCortyx.git" }
$Branch = $env:OPENCORTYX_BRANCH
$Channel = if ($env:OPENCORTYX_CHANNEL) { $env:OPENCORTYX_CHANNEL } else { "stable" }
$Dir = if ($env:OPENCORTYX_HOME) { $env:OPENCORTYX_HOME } else { Join-Path $HOME "opencortyx" }

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Die($msg) { Write-Host "x  $msg" -ForegroundColor Red; exit 1 }
# A native command whose stderr is noise or an expected failure; callers read $LASTEXITCODE.
# Windows PowerShell 5.1 turns redirected native stderr into a terminating error under
# $ErrorActionPreference = "Stop", so a stopped Docker threw instead of getting the message below.
function Invoke-Quiet {
    $ErrorActionPreference = "Continue"
    $exe = $args[0]
    $rest = @($args | Select-Object -Skip 1)
    & $exe @rest 2>$null
}
function Invoke-Cli($dir, $command) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dir "bin\opencortyx.ps1") $command
    if ($LASTEXITCODE -ne 0) { Die "opencortyx $command failed" }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Die "Install Docker Desktop first: https://docs.docker.com/desktop/setup/install/windows-install/" }
Invoke-Quiet docker info | Out-Null
if ($LASTEXITCODE -ne 0) { Die "Docker is not running. Start Docker Desktop and re-run the installer." }
Invoke-Quiet docker compose version | Out-Null
if ($LASTEXITCODE -ne 0) { Die "Docker Compose v2 is required." }
if ($Channel -notin @("stable", "beta")) { Die "OPENCORTYX_CHANNEL must be stable or beta, not '$Channel'." }

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
        $tags = Invoke-Quiet git ls-remote --tags --refs $Repo "v*" | ForEach-Object { ($_ -split "/")[-1] }
        # Newest first; a release sorts above its own betas (v1.0.0 > v1.0.0-beta.2).
        function Get-NewestTag($pattern) {
            $tags | Where-Object { $_ -match $pattern } |
                Sort-Object @{ e = { [version]($_ -replace '^v(\d+\.\d+\.\d+).*$', '$1') } },
                            @{ e = { if ($_ -match '-beta\.(\d+)$') { [int]$matches[1] } else { [int]::MaxValue } } } -Descending |
                Select-Object -First 1
        }
        $stable = '^v\d+\.\d+\.\d+$'
        $beta = '^v\d+\.\d+\.\d+(-beta\.\d+)?$'
        if ($Channel -eq "beta") {
            $Branch = Get-NewestTag $beta
        } else {
            $Branch = Get-NewestTag $stable
            if (-not $Branch) {
                $Branch = Get-NewestTag $beta
                if ($Branch) { $Channel = "beta"; Info "No stable release yet: installing beta $Branch" }
            }
        }
        if (-not $Branch) { $Branch = "main" }
    }
    $env:OPENCORTYX_CHANNEL = $Channel
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
