# マーケット監視の常駐アイコン（通知領域）。
# 15分ごとに GitHub Actions の5つの更新処理（NASDAQ100・S&P500・SOX・日経平均・Fear & Greed）を起動し、
# GitHub上の公開JSONの値と鮮度をアイコンの色とメニューで表示する。
# GitHubの予約実行は数時間遅れるため、その補助として動かす。gh のログインを使い、トークンは扱わない。
# Windows PowerShell 5.1 で日本語を読めるよう、UTF-8 BOM付きで保存する。
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$privateDir = Join-Path $repoRoot '.private'
$trayIconId = 28013   # install.ps1 と同じ値にする
$mutex = New-Object Threading.Mutex($false, 'Local\ChatGPTMarketMonitor.Tray')
$owned = $false
try { $owned = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned = $true }
if (-not $owned) { $mutex.Dispose(); exit 0 }

$exitCode = 0
$exitEvent = $null; $notify = $null; $menu = $null; $timer = $null; $icons = @{}; $sync = $null
$script:workerPs = $null; $script:workerHandle = $null
try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class MarketMonitorNative {
    [DllImport("user32.dll")] public static extern bool DestroyIcon(IntPtr handle);
    [StructLayout(LayoutKind.Sequential)] private struct Identifier {
        public uint cbSize; public IntPtr hWnd; public uint uID; public Guid guidItem;
    }
    [StructLayout(LayoutKind.Sequential)] private struct Rect { public int left, top, right, bottom; }
    [DllImport("shell32.dll")] private static extern int Shell_NotifyIconGetRect(ref Identifier identifier, out Rect rectangle);
    public static int[] GetRegisteredRect(IntPtr window, uint id) {
        var identifier = new Identifier { cbSize = (uint)Marshal.SizeOf(typeof(Identifier)), hWnd = window, uID = id };
        Rect rectangle;
        var result = Shell_NotifyIconGetRect(ref identifier, out rectangle);
        return new int[] { result, rectangle.left, rectangle.top, rectangle.right, rectangle.bottom };
    }
}
'@
    $corePath = Join-Path $PSScriptRoot 'tray-core.ps1'
    . $corePath
    if (-not (Test-Path -LiteralPath $privateDir)) { [void][IO.Directory]::CreateDirectory($privateDir) }
    $exitEvent = New-Object Threading.EventWaitHandle($false, [Threading.EventResetMode]::AutoReset, 'Local\ChatGPTMarketMonitor.Tray.Exit')
    $utf8 = New-Object Text.UTF8Encoding($false)

    $gh = $null
    $ghCommand = Get-Command gh.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($ghCommand) { $gh = $ghCommand.Source }
    elseif (Test-Path -LiteralPath "$env:ProgramFiles\GitHub CLI\gh.exe") { $gh = "$env:ProgramFiles\GitHub CLI\gh.exe" }

    $config = @{
        Repo = 'zzt372/chatgpt-market-monitor'
        Workflows = @('nasdaq-drawdown.yml', 'sp500-drawdown.yml', 'sox-drawdown.yml', 'nikkei-drawdown.yml', 'cnn-fear-greed.yml')
        Sources = @(
            @{ Name = 'NASDAQ100'; Path = 'monitors/nasdaq-drawdown/drawdown.json' },
            @{ Name = 'S&P500'; Path = 'monitors/sp500-drawdown/drawdown.json' },
            @{ Name = 'SOX'; Path = 'monitors/sox-drawdown/drawdown.json' },
            @{ Name = '日経平均'; Path = 'monitors/nikkei-drawdown/drawdown.json' },
            @{ Name = 'Fear & Greed'; Path = 'monitors/cnn-fear-greed/latest.json' }
        )
        DispatchMinutes = 15          # 更新処理を起動する間隔
        RetryMinutes = 3              # 起動に失敗したときの再試行
        RefreshMinutes = 5            # 公開JSONを読み直す間隔
        RefreshAfterDispatchSeconds = 150  # 起動後、GitHubでの処理が終わる頃に読み直す
        MaxFetchAgeHours = 3          # ChatGPT側が古いと判断する取得時刻の経過
        Gh = $gh; PrivateDir = $privateDir; CorePath = $corePath
    }
    $sync = [hashtable]::Synchronized(@{
        Stop = $false; Paused = $false; RunNow = $false; Phase = 'starting'
        ConsecutiveFailures = 0; LastDispatchAt = $null; LastDispatchErrors = @(); NextDispatchAt = $null
        Items = $null; ItemsUpdatedAt = $null; WorkerError = $null
    })

    # GitHubとの通信は別スレッド（ランスペース）で行い、アイコンの操作を止めない。
    $worker = {
        param($sync, $config)
        $ErrorActionPreference = 'Stop'
        . $config.CorePath
        $env:GH_NO_UPDATE_NOTIFIER = '1'; $env:GH_PROMPT_DISABLED = '1'; $env:NO_COLOR = '1'
        $utf8 = New-Object Text.UTF8Encoding($false)
        $logPath = Join-Path $config.PrivateDir 'tray.log'
        $statusPath = Join-Path $config.PrivateDir 'tray-status.json'

        function Write-TrayLog([string]$Message) {
            try {
                if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 262144) {
                    Move-Item -LiteralPath $logPath -Destination ($logPath + '.old') -Force
                }
                [IO.File]::AppendAllText($logPath, ('{0:yyyy-MM-dd HH:mm:ss} {1}' -f [DateTime]::Now, $Message) + "`r`n", $utf8)
            } catch {}
        }

        function Invoke-Gh([string[]]$Arguments, [int]$TimeoutSeconds) {
            $info = New-Object Diagnostics.ProcessStartInfo
            $info.FileName = $config.Gh
            $info.Arguments = ($Arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
            $info.UseShellExecute = $false; $info.CreateNoWindow = $true
            $info.RedirectStandardOutput = $true; $info.RedirectStandardError = $true
            $info.StandardOutputEncoding = $utf8; $info.StandardErrorEncoding = $utf8
            $process = [Diagnostics.Process]::Start($info)
            try {
                $out = $process.StandardOutput.ReadToEndAsync()
                $err = $process.StandardError.ReadToEndAsync()
                if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
                    try { $process.Kill() } catch {}
                    throw 'gh が応答しません'
                }
                $process.WaitForExit()
                $firstError = ([string]$err.Result).Trim() -split "`r?`n" | Select-Object -First 1
                return [pscustomobject]@{ ExitCode = $process.ExitCode; Out = $out.Result; Err = $firstError }
            } finally { $process.Dispose() }
        }

        function Invoke-Dispatch {
            $errors = New-Object Collections.Generic.List[string]
            foreach ($workflow in $config.Workflows) {
                try {
                    $result = Invoke-Gh @('workflow', 'run', $workflow, '-R', $config.Repo) 60
                    if ($result.ExitCode -ne 0) { $errors.Add("${workflow}: $($result.Err)") }
                } catch { $errors.Add("${workflow}: $($_.Exception.Message)") }
            }
            $sync.LastDispatchAt = [DateTime]::Now
            $sync.LastDispatchErrors = $errors.ToArray()
            if ($errors.Count -eq 0) {
                $sync.ConsecutiveFailures = 0
                Write-TrayLog ('更新処理を起動しました（{0}件）' -f $config.Workflows.Count)
            } else {
                $sync.ConsecutiveFailures = [int]$sync.ConsecutiveFailures + 1
                Write-TrayLog ('起動に失敗しました: ' + ($errors -join ' / '))
            }
            return ($errors.Count -eq 0)
        }

        function Update-Items {
            $now = [DateTime]::UtcNow
            $items = foreach ($source in $config.Sources) {
                $data = $null; $problem = $null
                try {
                    $result = Invoke-Gh @('api', '-H', 'Accept: application/vnd.github.raw+json', "repos/$($config.Repo)/contents/$($source.Path)") 30
                    if ($result.ExitCode -ne 0) { $problem = [string]$result.Err; if (-not $problem) { $problem = 'gh api failed' } }
                    else { $data = $result.Out | ConvertFrom-Json }
                } catch { $problem = $_.Exception.Message }
                Get-MarketItem -Name $source.Name -Data $data -ErrorText $problem -NowUtc $now -MaxAgeHours $config.MaxFetchAgeHours
            }
            $sync.Items = @($items)
            $sync.ItemsUpdatedAt = [DateTime]::Now
            $failed = @($items | Where-Object { $_.Failed })
            if ($failed.Count) { Write-TrayLog ('データを読めません: ' + (($failed | ForEach-Object { "$($_.Name) ($($_.Detail))" }) -join ', ')) }
        }

        function Save-Status {
            try {
                $last = $null; $next = $null
                if ($sync.LastDispatchAt) { $last = $sync.LastDispatchAt.ToString('o') }
                if ($sync.NextDispatchAt) { $next = $sync.NextDispatchAt.ToString('o') }
                $items = @($sync.Items | Where-Object { $_ } | ForEach-Object { [ordered]@{ name = $_.Name; text = $_.Text; stale = $_.Stale; failed = $_.Failed } })
                $status = [ordered]@{
                    pid = $PID; paused = $sync.Paused; lastDispatchAt = $last; nextDispatchAt = $next
                    consecutiveFailures = $sync.ConsecutiveFailures; lastDispatchErrors = @($sync.LastDispatchErrors)
                    items = $items; updatedAt = [DateTime]::Now.ToString('o')
                }
                [IO.File]::WriteAllText($statusPath, ($status | ConvertTo-Json -Depth 4), $utf8)
            } catch {}
        }

        $nextRefresh = [DateTime]::Now.AddSeconds(2)
        $nextDispatch = [DateTime]::Now.AddSeconds(15)   # ログイン直後はネット接続を少し待つ
        $sync.Phase = 'idle'
        Write-TrayLog '常駐アイコンを起動しました'
        while (-not $sync.Stop) {
            try {
                if ($config.Gh) {
                    $forced = [bool]$sync.RunNow
                    if ($forced) { $sync.RunNow = $false }
                    if ($forced -or (-not $sync.Paused -and [DateTime]::Now -ge $nextDispatch)) {
                        $nextDispatch = [DateTime]::Now.AddMinutes($config.RetryMinutes)
                        $sync.Phase = 'dispatching'
                        try { $ok = Invoke-Dispatch } finally { $sync.Phase = 'idle' }
                        if ($ok) { $nextDispatch = [DateTime]::Now.AddMinutes($config.DispatchMinutes) }
                        $nextRefresh = [DateTime]::Now.AddSeconds($config.RefreshAfterDispatchSeconds)
                        if ($sync.Paused) { $sync.NextDispatchAt = $null } else { $sync.NextDispatchAt = $nextDispatch }
                        Save-Status
                    }
                    if ($sync.Paused) { $sync.NextDispatchAt = $null } else { $sync.NextDispatchAt = $nextDispatch }
                    if ([DateTime]::Now -ge $nextRefresh) {
                        $nextRefresh = [DateTime]::Now.AddMinutes($config.RefreshMinutes)
                        Update-Items
                        Save-Status
                    }
                }
                $sync.WorkerError = $null
            } catch {
                $sync.WorkerError = $_.Exception.Message
                Write-TrayLog ('内部エラー: ' + $_.Exception.Message)
            }
            for ($i = 0; $i -lt 10 -and -not $sync.Stop -and -not $sync.RunNow; $i++) { Start-Sleep -Milliseconds 100 }
        }
        Write-TrayLog '常駐アイコンを終了しました'
    }

    $script:workerFailed = $false
    $script:workerStartedAt = [DateTime]::MinValue
    function Start-Worker {
        if ($script:workerPs) {
            try { $script:workerPs.Runspace.Dispose(); $script:workerPs.Dispose() } catch {}
        }
        $runspace = [runspacefactory]::CreateRunspace()
        $runspace.Open()
        $script:workerPs = [powershell]::Create()
        $script:workerPs.Runspace = $runspace
        [void]$script:workerPs.AddScript($worker.ToString()).AddArgument($sync).AddArgument($config)
        $script:workerHandle = $script:workerPs.BeginInvoke()
        $script:workerStartedAt = [DateTime]::Now
    }

    [Windows.Forms.Application]::EnableVisualStyles()
    # 藍色の角丸四角に白いろうそく足（株価チャート）。右上の点の色で状態を表す。
    # 丸い緑のアイコン（ちかめし等）と見分けられるよう、形と地の色を変えている。
    function New-MarketIcon([Drawing.Color]$Dot) {
        $tile = [Drawing.Color]::FromArgb(67, 56, 202)
        $bitmap = New-Object Drawing.Bitmap(64, 64)
        $graphics = [Drawing.Graphics]::FromImage($bitmap)
        $tileBrush = New-Object Drawing.SolidBrush($tile)
        $dotBrush = New-Object Drawing.SolidBrush($Dot)
        $white = New-Object Drawing.SolidBrush([Drawing.Color]::White)
        $wick = New-Object Drawing.Pen([Drawing.Color]::White, 3)
        $shape = New-Object Drawing.Drawing2D.GraphicsPath
        $handle = [IntPtr]::Zero
        try {
            $graphics.SmoothingMode = [Drawing.Drawing2D.SmoothingMode]::AntiAlias
            $graphics.Clear([Drawing.Color]::Transparent)
            $shape.AddArc(2, 2, 28, 28, 180, 90); $shape.AddArc(34, 2, 28, 28, 270, 90)
            $shape.AddArc(34, 34, 28, 28, 0, 90); $shape.AddArc(2, 34, 28, 28, 90, 90)
            $shape.CloseFigure()
            $graphics.FillPath($tileBrush, $shape)
            # 右下がりに並ぶ3本のろうそく足: x, ひげ上端, ひげ下端, 実体上端, 実体下端
            foreach ($candle in @(@(14, 13, 45, 19, 35), @(30, 23, 53, 29, 45), @(46, 35, 59, 39, 53))) {
                $graphics.DrawLine($wick, $candle[0], $candle[1], $candle[0], $candle[2])
                $graphics.FillRectangle($white, $candle[0] - 6, $candle[3], 12, $candle[4] - $candle[3])
            }
            $graphics.FillEllipse($tileBrush, 34, 3, 27, 27)
            $graphics.FillEllipse($dotBrush, 38, 7, 19, 19)
            $handle = $bitmap.GetHicon()
            $borrowed = [Drawing.Icon]::FromHandle($handle)
            $result = $borrowed.Clone()
            $borrowed.Dispose()
            return $result
        } finally {
            if ($handle -ne [IntPtr]::Zero) { [void][MarketMonitorNative]::DestroyIcon($handle) }
            $shape.Dispose(); $wick.Dispose(); $white.Dispose(); $dotBrush.Dispose(); $tileBrush.Dispose()
            $graphics.Dispose(); $bitmap.Dispose()
        }
    }
    $colors = @{
        green = [Drawing.Color]::FromArgb(34, 197, 94); blue = [Drawing.Color]::FromArgb(56, 189, 248)
        amber = [Drawing.Color]::FromArgb(245, 158, 11); red = [Drawing.Color]::FromArgb(239, 68, 68)
        gray = [Drawing.Color]::FromArgb(148, 163, 184)
    }
    foreach ($name in $colors.Keys) { $icons[$name] = New-MarketIcon $colors[$name] }

    $menu = New-Object Windows.Forms.ContextMenuStrip
    $menu.Font = New-Object Drawing.Font('Yu Gothic UI', 10)
    $titleItem = $menu.Items.Add('マーケット監視'); $titleItem.Enabled = $false
    $statusItem = $menu.Items.Add('状態を確認しています…'); $statusItem.Enabled = $false
    $scheduleItem = $menu.Items.Add('前回の起動 なし'); $scheduleItem.Enabled = $false
    [void]$menu.Items.Add((New-Object Windows.Forms.ToolStripSeparator))
    $captionItem = $menu.Items.Add('指数は直近3年の最高値（終値）からの下落率'); $captionItem.Enabled = $false
    $dataItems = @(foreach ($source in $config.Sources) {
        $item = $menu.Items.Add($source.Name.Replace('&', '&&') + '　確認中…'); $item.Enabled = $false; $item
    })
    [void]$menu.Items.Add((New-Object Windows.Forms.ToolStripSeparator))
    $runNowItem = $menu.Items.Add('今すぐ更新')
    $pauseItem = $menu.Items.Add('一時停止')
    $actionsItem = $menu.Items.Add('GitHubの実行履歴を開く')
    [void]$menu.Items.Add((New-Object Windows.Forms.ToolStripSeparator))
    $exitItem = $menu.Items.Add('終了')

    $notify = New-Object Windows.Forms.NotifyIcon
    # Windowsはアイコンの表示設定を「実行ファイル＋番号」で覚える。PowerShell製のアイコンは既定で
    # どれも番号1になり設定を共有してしまうため、固有の番号を使う（install.ps1 が常時表示に設定する）。
    try { [Windows.Forms.NotifyIcon].GetField('id', [Reflection.BindingFlags]'Instance,NonPublic').SetValue($notify, $trayIconId) } catch {}
    $notify.ContextMenuStrip = $menu
    $notify.Icon = $icons.blue
    $notify.Text = 'マーケット監視 · 起動しています'
    $notify.Visible = $true

    $script:lastColor = 'blue'
    $script:lastTip = ''
    $script:uiDiagnosticSaved = $false
    function Update-Tray {
        $state = @{
            GhPath = $config.Gh; WorkerFailed = $script:workerFailed; Paused = $sync.Paused; Phase = $sync.Phase
            ConsecutiveFailures = $sync.ConsecutiveFailures; LastDispatchAt = $sync.LastDispatchAt; Items = $sync.Items
        }
        $status = Get-MonitorStatus -State $state -NowLocal ([DateTime]::Now)
        if ($status.Color -ne $script:lastColor) {
            $notify.Icon = $icons[$status.Color]
            if ($status.Color -eq 'red') {
                $notify.ShowBalloonTip(10000, 'マーケット監視', $status.Label, [Windows.Forms.ToolTipIcon]::Warning)
            }
            $script:lastColor = $status.Color
        }
        $tip = 'マーケット監視 · ' + $status.Label
        if ($tip.Length -gt 63) { $tip = $tip.Substring(0, 63) }
        if ($tip -ne $script:lastTip) { $notify.Text = $tip; $script:lastTip = $tip }
        $statusItem.Text = $status.Label
        $scheduleItem.Text = Format-Schedule -LastDispatchAt $sync.LastDispatchAt -NextDispatchAt $sync.NextDispatchAt -Paused ([bool]$sync.Paused)
        $items = @($sync.Items)
        for ($i = 0; $i -lt $dataItems.Count; $i++) {
            if ($i -lt $items.Count -and $items[$i]) { $dataItems[$i].Text = $items[$i].Text.Replace('&', '&&') }
        }
        if ($sync.Paused) { $pauseItem.Text = '再開' } else { $pauseItem.Text = '一時停止' }
        $runNowItem.Enabled = [bool]$config.Gh -and $sync.Phase -ne 'dispatching'
        if (-not $script:uiDiagnosticSaved) {
            $script:uiDiagnosticSaved = $true
            try {
                $flags = [Reflection.BindingFlags]'Instance,NonPublic'
                $window = [Windows.Forms.NotifyIcon].GetField('window', $flags).GetValue($notify)
                $id = [Windows.Forms.NotifyIcon].GetField('id', $flags).GetValue($notify)
                $rect = [MarketMonitorNative]::GetRegisteredRect($window.Handle, $id)
                $diagnostic = [ordered]@{ pid = $PID; iconVisible = $notify.Visible; shellRegistered = ($rect[0] -eq 0); iconRect = @($rect[1], $rect[2], $rect[3], $rect[4]); gh = $config.Gh; savedAt = [DateTime]::Now.ToString('o') }
                [IO.File]::WriteAllText((Join-Path $privateDir 'tray-ui.json'), ($diagnostic | ConvertTo-Json -Compress), $utf8)
            } catch {}
        }
    }

    $openActions = { Start-Process ('https://github.com/' + $config.Repo + '/actions') }
    $actionsItem.add_Click($openActions)
    $notify.add_DoubleClick($openActions)
    $runNowItem.add_Click({ $sync.RunNow = $true; $sync.Phase = 'dispatching'; Update-Tray })
    $pauseItem.add_Click({ $sync.Paused = -not $sync.Paused; Update-Tray })
    $exitItem.add_Click({ [Windows.Forms.Application]::ExitThread() })
    $menu.add_Opening({ try { Update-Tray } catch {} })

    $timer = New-Object Windows.Forms.Timer
    $timer.Interval = 1000
    $timer.add_Tick({
        if ($exitEvent.WaitOne(0)) { [Windows.Forms.Application]::ExitThread(); return }
        try {
            if ($script:workerHandle.IsCompleted -and -not $sync.Stop) {
                $script:workerFailed = $true
                if (([DateTime]::Now - $script:workerStartedAt).TotalSeconds -ge 60) { Start-Worker; $script:workerFailed = $false }
            }
            Update-Tray
        } catch {}
    })
    Start-Worker
    $timer.Start()
    [Windows.Forms.Application]::Run()
} catch {
    $exitCode = 1
    try {
        if (-not (Test-Path -LiteralPath $privateDir)) { [void][IO.Directory]::CreateDirectory($privateDir) }
        $message = '{0:yyyy-MM-dd HH:mm:ss} 常駐アイコンを起動できません: {1}: {2}' -f [DateTime]::Now, $_.Exception.GetType().FullName, $_.Exception.Message
        [IO.File]::AppendAllText((Join-Path $privateDir 'tray.log'), $message + "`r`n", (New-Object Text.UTF8Encoding($false)))
    } catch {}
} finally {
    if ($sync) { $sync.Stop = $true }
    if ($timer) { $timer.Stop(); $timer.Dispose() }
    if ($notify) { $notify.Visible = $false; $notify.Dispose() }
    if ($menu) { $menu.Dispose() }
    foreach ($icon in $icons.Values) { $icon.Dispose() }
    if ($script:workerHandle) { try { [void]$script:workerHandle.AsyncWaitHandle.WaitOne(3000) } catch {} }
    if ($exitEvent) { $exitEvent.Dispose() }
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
# gh の応答待ちが残っていても、通信用スレッドごと確実に終了する。
[Environment]::Exit($exitCode)
