[CmdletBinding()]
param(
    [string]$Config,
    [ValidateRange(1, 65535)]
    [int]$ApiPort = 8000,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 3000,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$configPath = $null

if ($Config) {
    $configPath = (Resolve-Path -LiteralPath $Config).Path
}

if ($ApiPort -eq $WebPort) {
    throw 'API and web ports must be different.'
}

foreach ($port in @($ApiPort, $WebPort)) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) {
        throw "Port $port is already in use (PID $($listener.OwningProcess)). Close the old monitor windows or choose different -ApiPort/-WebPort values."
    }
}

$apiCommand = "python -m orches api --host 127.0.0.1 --port $ApiPort"
if ($configPath) {
    $apiCommand += " --config `"$configPath`""
}

Start-Process powershell.exe `
    -WorkingDirectory $repo `
    -ArgumentList @('-NoExit', '-Command', $apiCommand) | Out-Null

$schema = $null
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
        $schema = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/openapi.json" -TimeoutSec 2
        break
    } catch {
        Start-Sleep -Milliseconds 250
    }
}
if (-not $schema) {
    throw "API did not start on port $ApiPort. Check the API PowerShell window before starting the web client."
}
if (-not $schema.paths.'/api/projects'.post) {
    throw "API on port $ApiPort is outdated: POST /api/projects is missing. Close the old API window and restart the monitor."
}

$webCommand = "`$env:NUXT_API_PROXY_URL='http://127.0.0.1:$ApiPort'; npm run dev -- --host=127.0.0.1 --port=$WebPort --strictPort"
Start-Process powershell.exe `
    -WorkingDirectory (Join-Path $repo 'web') `
    -ArgumentList @('-NoExit', '-Command', $webCommand) | Out-Null

$webReady = $false
for ($attempt = 0; $attempt -lt 80; $attempt++) {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$WebPort/" -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            $webReady = $true
            break
        }
    } catch {
        Start-Sleep -Milliseconds 250
    }
}
if (-not $webReady) {
    throw "Web client did not start on port $WebPort. Check the web PowerShell window."
}

if (-not $NoBrowser) {
    Start-Process "http://localhost:$WebPort"
}

Write-Host "Orches API: http://127.0.0.1:$ApiPort"
Write-Host "Orches web: http://localhost:$WebPort"
Write-Host 'Close the opened PowerShell windows or press Ctrl+C in them to stop.'
