param(
    [string]$GlobalRoot = "C:\CodexGlobal",
    [string]$ConfigPath = "",
    [switch]$Verify
)

$ErrorActionPreference = "Stop"
$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$globalRoot = [IO.Path]::GetFullPath($GlobalRoot)
$agentsPath = Join-Path $globalRoot "AGENTS.md"
$architectureSource = Join-Path $repositoryRoot "integration\codex-global\dual-agents-architecture.md"
$skillSource = Join-Path $repositoryRoot "skills\dual-agents\SKILL.md"
$launcherSource = Join-Path $repositoryRoot "scripts\global-dual-codex.ps1"
$manifestPath = Join-Path $globalRoot "dual-agents-integration.json"
$skillDestination = Join-Path $globalRoot "skills\dual-agents\SKILL.md"
$wrapperDestination = Join-Path $globalRoot "bin\dual-codex.ps1"

function Normalize-Lf([string]$Text) {
    return ($Text -replace "`r`n?", "`n")
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Write-AtomicText([string]$Path, [string]$Text) {
    $parent = Split-Path -Parent $Path
    $temporary = Join-Path $parent ("." + [IO.Path]::GetFileName($Path) + "." + [guid]::NewGuid().ToString("N") + ".tmp")
    try {
        [IO.File]::WriteAllText($temporary, $Text, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Resolve-ConfigPath([string]$RequestedPath) {
    $candidate = $RequestedPath
    if (-not $candidate -and $env:DUAL_CODEX_CONFIG) { $candidate = $env:DUAL_CODEX_CONFIG }
    if (-not $candidate -and (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        $existing = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $candidate = [string]$existing.config_path
    }
    if (-not $candidate) { throw "Pass -ConfigPath once or set DUAL_CODEX_CONFIG to the existing Dual Agents config path." }
    $resolved = Resolve-Path -LiteralPath $candidate -ErrorAction Stop
    if ($resolved.Provider.Name -ne "FileSystem" -or (Get-Item -LiteralPath $resolved.Path).PSIsContainer) {
        throw "ConfigPath must name an existing file."
    }
    return [IO.Path]::GetFullPath($resolved.Path)
}

if (-not (Test-Path -LiteralPath $agentsPath -PathType Leaf)) {
    throw "Canonical global AGENTS.md was not found at '$agentsPath'."
}
foreach ($source in @($architectureSource, $skillSource, $launcherSource)) {
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Required repo-owned source is missing: $source" }
}

$configPathResolved = Resolve-ConfigPath $ConfigPath
$architecture = (Normalize-Lf (Get-Content -LiteralPath $architectureSource -Raw -Encoding UTF8)).TrimEnd("`n")
$architectureHash = Get-Sha256 $architectureSource
$skillHash = Get-Sha256 $skillSource
$launcherHash = Get-Sha256 $launcherSource

$agentsText = Normalize-Lf (Get-Content -LiteralPath $agentsPath -Raw -Encoding UTF8)
$sectionHeadings = [regex]::Matches($agentsText, '(?m)^## Dual Agents architecture[ \t]*$')
$sharedAnchors = [regex]::Matches($agentsText, '(?m)^## Shared global instructions\b[^\n]*$')
$beginMarker = '<!-- DUAL_AGENTS_GLOBAL_ARCHITECTURE_BEGIN -->'
$endMarker = '<!-- DUAL_AGENTS_GLOBAL_ARCHITECTURE_END -->'
$beginMarkers = [regex]::Matches($agentsText, [regex]::Escape($beginMarker))
$endMarkers = [regex]::Matches($agentsText, [regex]::Escape($endMarker))

if ($sectionHeadings.Count -gt 1) { throw "Global AGENTS.md has multiple 'Dual Agents architecture' sections; refusing an ambiguous update." }
if ($sharedAnchors.Count -ne 1) { throw "Expected exactly one canonical 'Shared global instructions' anchor in global AGENTS.md." }
if ($beginMarkers.Count -ne $endMarkers.Count -or $beginMarkers.Count -gt 1) {
    throw "Global AGENTS.md has conflicting or partial Dual Agents architecture markers."
}

$sectionMatch = $null
$currentSection = $null
if ($sectionHeadings.Count -eq 0) {
    if ($beginMarkers.Count -ne 0) { throw "Global AGENTS.md has Dual Agents markers without an architecture section." }
} else {
    $sectionMatch = $sectionHeadings[0]
    $sharedAnchor = $sharedAnchors[0]
    if ($sectionMatch.Index -ge $sharedAnchor.Index) {
        throw "The Dual Agents architecture section must appear before the shared global instructions anchor."
    }
    $currentSection = $agentsText.Substring($sectionMatch.Index, $sharedAnchor.Index - $sectionMatch.Index).TrimEnd("`n")
    if ($beginMarkers.Count -eq 1) {
        $beginIndex = $beginMarkers[0].Index
        $endIndex = $endMarkers[0].Index
        if ($beginIndex -lt $sectionMatch.Index -or $endIndex -lt $beginIndex -or $endIndex -ge $sharedAnchor.Index) {
            throw "Global AGENTS.md has misplaced or corrupt Dual Agents architecture markers."
        }
    }
}
$manifest = [ordered]@{
    schema_version = 1
    repository_root = $repositoryRoot
    config_path = $configPathResolved
    architecture_sha256 = $architectureHash
    skill_sha256 = $skillHash
    launcher_sha256 = $launcherHash
}
$manifestText = ($manifest | ConvertTo-Json -Depth 4) + "`n"

$filesToVerify = @(
    @{ Path = $skillDestination; Source = $skillSource; Hash = $skillHash },
    @{ Path = $wrapperDestination; Source = $launcherSource; Hash = $launcherHash }
)

if (-not $Verify) {
    foreach ($file in $filesToVerify) {
        $marker = $file.Path + ".dual-agents-managed"
        if ((Test-Path -LiteralPath $file.Path) -and -not (Test-Path -LiteralPath $marker)) {
            throw "Refusing to overwrite an unmarked global file: $($file.Path)"
        }
    }
}

if ($Verify) {
    if ($null -eq $currentSection -or $currentSection -ne $architecture) { throw "Global Dual Agents architecture differs from the repo-owned source." }
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "Global integration manifest is missing." }
    $currentManifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($currentManifest.repository_root -ne $repositoryRoot -or $currentManifest.config_path -ne $configPathResolved) {
        throw "Global integration registration points to a different repository or config."
    }
    foreach ($file in $filesToVerify) {
        $marker = $file.Path + ".dual-agents-managed"
        if (-not (Test-Path -LiteralPath $file.Path -PathType Leaf) -or -not (Test-Path -LiteralPath $marker -PathType Leaf)) {
            throw "Managed integration file is missing: $($file.Path)"
        }
        if ((Get-Sha256 $file.Path) -ne $file.Hash -or (Get-Content -LiteralPath $marker -Raw -Encoding UTF8).Trim() -ne $file.Hash) {
            throw "Managed integration file differs from its repo-owned source: $($file.Path)"
        }
    }
    if ((Normalize-Lf (Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8)) -ne (Normalize-Lf $manifestText)) {
        throw "Global integration manifest differs from the repo-owned registration."
    }
    Write-Output "Dual Agents global integration verified."
    exit 0
}

$backupPath = Join-Path $globalRoot "AGENTS.md.pre-dual-agents.bak"
if ($currentSection -ne $architecture -and $currentSection -notmatch '<!-- DUAL_AGENTS_GLOBAL_ARCHITECTURE_BEGIN -->') {
    if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
        Copy-Item -LiteralPath $agentsPath -Destination $backupPath
    }
}

foreach ($directory in @((Split-Path -Parent $skillDestination), (Split-Path -Parent $wrapperDestination))) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
foreach ($file in $filesToVerify) {
    $marker = $file.Path + ".dual-agents-managed"
    $sourceText = Get-Content -LiteralPath $file.Source -Raw -Encoding UTF8
    Write-AtomicText $file.Path $sourceText
    Write-AtomicText $marker ($file.Hash + "`n")
}

$newline = if ((Get-Content -LiteralPath $agentsPath -Raw -Encoding UTF8).Contains("`r`n")) { "`r`n" } else { "`n" }
$globalArchitecture = $architecture -replace "`n", $newline
$replacement = $globalArchitecture + $newline + $newline
if ($null -ne $sectionMatch) {
    $updatedAgents = $agentsText.Substring(0, $sectionMatch.Index) + $replacement + $agentsText.Substring($sharedAnchor.Index)
} else {
    $insertionIndex = $sharedAnchors[0].Index
    $prefix = $agentsText.Substring(0, $insertionIndex)
    if ($prefix.EndsWith("`n`n")) { $separatorBefore = "" }
    elseif ($prefix.EndsWith("`n")) { $separatorBefore = "`n" }
    else { $separatorBefore = "`n`n" }
    $updatedAgents = $prefix + $separatorBefore + $globalArchitecture + $newline + $newline + $agentsText.Substring($insertionIndex)
}
Write-AtomicText $agentsPath $updatedAgents
Write-AtomicText $manifestPath $manifestText
Write-Output "Dual Agents global integration installed from '$repositoryRoot'."
