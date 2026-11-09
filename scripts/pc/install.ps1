# Windowsログイン時に常駐アイコンを自動起動する設定と、その解除。
#   登録して起動: powershell -ExecutionPolicy Bypass -File scripts\pc\install.ps1
#   解除して終了: powershell -ExecutionPolicy Bypass -File scripts\pc\install.ps1 -Uninstall
# Windows PowerShell 5.1 で日本語を読めるよう、UTF-8 BOM付きで保存する。
param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$launcher = Join-Path $repoRoot 'マーケット監視を起動.vbs'
$wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
$shortcutPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'ChatGPT-Market-Monitor-Tray.lnk'

if ($Uninstall) {
    & (Join-Path $PSScriptRoot 'stop.ps1')
    if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath }
    Write-Output '自動起動を解除しました。'
    return
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $wscript
$shortcut.Arguments = '"' + $launcher + '"'
$shortcut.WorkingDirectory = $repoRoot
$shortcut.WindowStyle = 7
$shortcut.Description = 'マーケット監視の常駐アイコン'
$shortcut.Save()
Write-Output "自動起動を登録しました: $shortcutPath"
Start-Process -FilePath $wscript -ArgumentList ('"' + $launcher + '"')
Write-Output '常駐アイコンを起動しました。'

# 隠れているインジケーター（^）ではなく、タスクバーに常に表示する。
# Windowsが初めてアイコンを見たときに作る設定（実行ファイル＋番号 28013）だけを変更する。
$trayIconId = 28013
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    $entry = Get-ChildItem 'HKCU:\Control Panel\NotifyIconSettings' -ErrorAction SilentlyContinue |
        Where-Object { $p = Get-ItemProperty $_.PSPath; $p.UID -eq $trayIconId -and $p.ExecutablePath -like '*\powershell.exe' } |
        Select-Object -First 1
    if ($entry) {
        if ((Get-ItemProperty $entry.PSPath).IsPromoted -ne 1) {
            Set-ItemProperty -LiteralPath $entry.PSPath -Name IsPromoted -Value 1 -Type DWord
        }
        Write-Output 'タスクバーに常に表示する設定にしました。'
        break
    }
    Start-Sleep -Seconds 1
}
if (-not $entry) { Write-Output 'アイコンの表示設定が見つかりません。タスクバーの「^」からドラッグして表示してください。' }
