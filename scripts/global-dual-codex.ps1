param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
$globalRoot = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $globalRoot "dual-agents-integration.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Dual Agents integration is not registered. Run scripts/install-dual-agents-integration.ps1 once."
}

$integration = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($integration.schema_version -ne 1) {
    throw "Unsupported Dual Agents integration manifest version."
}

$repositoryRoot = [string]$integration.repository_root
$configPath = [string]$integration.config_path
$launcher = Join-Path $repositoryRoot "scripts\dual-codex.ps1"
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "The registered Dual Agents repository has no scripts/dual-codex.ps1 launcher. Update the global integration."
}
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "The registered Dual Agents config is unavailable. Update the global integration."
}

if ($Arguments -contains "--config" -or ($Arguments | Where-Object { $_ -like "--config=*" })) {
    throw "The global launcher uses its registered config. Update the registration to change it."
}
if ($Arguments.Count -gt 0 -and $Arguments[0] -eq "run") {
    $repositoryIndex = [Array]::IndexOf($Arguments, "--repository")
    if ($repositoryIndex -lt 0 -or $repositoryIndex + 1 -ge $Arguments.Count -or -not $Arguments[$repositoryIndex + 1]) {
        throw "Global run requires an explicit --repository target."
    }
}

& $launcher --config $configPath @Arguments
exit $LASTEXITCODE
