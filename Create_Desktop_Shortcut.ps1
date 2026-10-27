param(
    [string]$DestinationFolder = [Environment]::GetFolderPath('Desktop')
)

$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'Start_Sync_Recorder.ps1'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Launcher not found: $launcher"
}
if (-not (Test-Path -LiteralPath $DestinationFolder -PathType Container)) {
    throw "Shortcut destination does not exist: $DestinationFolder"
}

$shortcutPath = Join-Path $DestinationFolder 'Seek FLIR Ultramic Recorder.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $PSHOME 'powershell.exe'
$shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $launcher + '"'
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Description = 'Start the Seek / FLIR / Ultramic Recorder'
$shortcut.WindowStyle = 7
$appIcon = Join-Path $PSScriptRoot 'SyncRecorder.exe'
if (Test-Path -LiteralPath $appIcon -PathType Leaf) {
    $shortcut.IconLocation = "$appIcon,0"
}
$shortcut.Save()
Write-Host "Created desktop shortcut: $shortcutPath"
