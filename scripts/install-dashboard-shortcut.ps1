[CmdletBinding()]
param(
    [string]$RepositoryRoot
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = if ($RepositoryRoot) {
    $RepositoryRoot
} else {
    (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
}
$repository = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$launcher = Join-Path $repository "scripts\dashboard-launcher.cmd"
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Dashboard launcher was not found: $launcher"
}

$desktop = [Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop)
if ([string]::IsNullOrWhiteSpace($desktop)) {
    throw "Windows Desktop path could not be resolved."
}
$shortcutPath = Join-Path $desktop "Dual Agents Dashboard.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $env:SystemRoot "System32\cmd.exe"
$shortcut.Arguments = '/d /s /c ""' + $launcher + '""'
$shortcut.WorkingDirectory = $repository
$shortcut.Description = "Launch the Dual Agents dashboard"
$shortcut.IconLocation = "$(Join-Path $env:SystemRoot 'System32\shell32.dll'),13"
$shortcut.Save()

[pscustomobject]@{
    shortcut = $shortcutPath
    target = $shortcut.TargetPath
    arguments = $shortcut.Arguments
    working_directory = $shortcut.WorkingDirectory
    dashboard_url = "http://127.0.0.1:<dynamic-port>/"
} | ConvertTo-Json -Compress
