#requires -Version 7.0

<#
.SYNOPSIS
    Launches Agents Playground using values from a365.generated.config.json.

.DESCRIPTION
    Reads the generated Agent 365 configuration file, validates the required
    authentication settings, and starts the Agents Playground against the
    configured bot messaging endpoint.

.EXAMPLE
    .\scripts\Start-AgentsPlayground.ps1

.EXAMPLE
    .\scripts\Start-AgentsPlayground.ps1 -ChannelId webchat

.EXAMPLE
    .\scripts\Start-AgentsPlayground.ps1 -WhatIf
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ConfigPath,
    [ValidateNotNullOrEmpty()]
    [string]$ChannelId = "emulator",
    [switch]$Wait
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $ConfigPath) {
    $ConfigPath = Join-Path $repoRoot "a365.generated.config.json"
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Config file not found: $ConfigPath"
}

$agentsPlaygroundCommand = Get-Command agentsplayground -ErrorAction SilentlyContinue
if (-not $agentsPlaygroundCommand) {
    throw "The 'agentsplayground' command was not found. Install it with 'winget install agentsplayground' or 'npm install -g @microsoft/m365agentsplayground'."
}

try {
    $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
}
catch {
    throw "Failed to read or parse config file '$ConfigPath': $($_.Exception.Message)"
}

$endpoint = $config.botMessagingEndpoint
$clientId = if ($config.botMsaAppId) { $config.botMsaAppId } else { $config.agentBlueprintId }
$tenantId = $config.tenantId
$clientSecret = $config.agentBlueprintClientSecret

$missingValues = @()
if ([string]::IsNullOrWhiteSpace($endpoint)) { $missingValues += "botMessagingEndpoint" }
if ([string]::IsNullOrWhiteSpace($clientId)) { $missingValues += "botMsaAppId/agentBlueprintId" }
if ([string]::IsNullOrWhiteSpace($tenantId)) { $missingValues += "tenantId" }
if ([string]::IsNullOrWhiteSpace($clientSecret)) { $missingValues += "agentBlueprintClientSecret" }

if ($missingValues.Count -gt 0) {
    throw "Missing required config value(s): $($missingValues -join ', ')"
}

$argumentList = @(
    "-e", $endpoint,
    "-c", $ChannelId,
    "--client-id", $clientId,
    "--client-secret", $clientSecret,
    "--tenant-id", $tenantId
)

Write-Host "Config file: $ConfigPath" -ForegroundColor Cyan
Write-Host "Endpoint:    $endpoint" -ForegroundColor Cyan
Write-Host "Channel:     $ChannelId" -ForegroundColor Cyan
Write-Host "Client ID:   $clientId" -ForegroundColor Cyan
Write-Host "Tenant ID:   $tenantId" -ForegroundColor Cyan
Write-Host ""

if ($PSCmdlet.ShouldProcess("Agents Playground", "Launch with config from $ConfigPath")) {
    if ($Wait) {
        & $agentsPlaygroundCommand.Source @argumentList
        exit $LASTEXITCODE
    }

    Start-Process -FilePath $agentsPlaygroundCommand.Source -ArgumentList $argumentList | Out-Null
    Write-Host "Agents Playground launched." -ForegroundColor Green
}
