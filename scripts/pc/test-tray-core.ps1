# tray-core.ps1 の表示内容と状態判定の検証。
#   powershell -ExecutionPolicy Bypass -File scripts\pc\test-tray-core.ps1
# Windows PowerShell 5.1 で日本語を読めるよう、UTF-8 BOM付きで保存する。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'tray-core.ps1')
$script:failures = 0
function Assert-Same($Expected, $Actual, [string]$Name) {
    if ([string]$Expected -ceq [string]$Actual) { Write-Output "ok   $Name" }
    else { Write-Output "FAIL $Name`n     expected: $Expected`n     actual:   $Actual"; $script:failures++ }
}
function Utc([string]$Text) {
    [DateTime]::Parse($Text, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
}
$now = Utc '2026-11-09T13:20:00Z'
$minus = [string][char]0x2212

# 指数の1行: 252日高値比・終値の日付・取得からの経過
$ndx = [pscustomobject]@{ ok = $true; rolling_drawdown_pct = -13.562024154603296; current_date = '2026-11-06'; fetched_at = '2026-11-09T13:07:59.123456Z' }
$item = Get-MarketItem -Name 'NASDAQ100' -Data $ndx -NowUtc $now
Assert-Same "NASDAQ100　${minus}13.56%（11/6終値・12分前に取得）" $item.Text 'index line'
Assert-Same $false $item.Stale 'fresh index is not stale'
$justNow = [pscustomobject]@{ ok = $true; rolling_drawdown_pct = -13.562; current_date = '2026-11-06'; fetched_at = '2026-11-09T13:19:30Z' }
Assert-Same "NASDAQ100　${minus}13.56%（11/6終値・たった今取得）" (Get-MarketItem -Name 'NASDAQ100' -Data $justNow -NowUtc $now).Text 'just fetched line'

# 3時間以上前の取得は古い（ChatGPT側の判定保留と同じ基準）
$old = [pscustomobject]@{ ok = $true; rolling_drawdown_pct = -35.8358; current_date = '2026-11-06'; fetched_at = '2026-11-09T10:20:00Z' }
$item = Get-MarketItem -Name 'SOX' -Data $old -NowUtc $now
Assert-Same $true $item.Stale 'exactly 3 hours is stale'
Assert-Same "SOX　${minus}35.84%（11/6終値・3時間前に取得）・古い" $item.Text 'stale index line'

# Fear & Greed はスコアとカテゴリの日本語
$cnn = [pscustomobject]@{ ok = $true; score = 24.2809523809524; category = 'Extreme Fear'; fetched_at = '2026-11-09T13:15:13.02349Z' }
Assert-Same 'Fear & Greed　24.3 極度の恐怖（4分前に取得）' (Get-MarketItem -Name 'Fear & Greed' -Data $cnn -NowUtc $now).Text 'fear and greed line'

# 読込失敗・不正データ
$failed = Get-MarketItem -Name 'SOX' -Data $null -ErrorText 'HTTP 404' -NowUtc $now
Assert-Same $true $failed.Failed 'read error is failed'
Assert-Same 'SOX　読み込めません' $failed.Text 'read error line'
$bad = Get-MarketItem -Name 'SOX' -Data ([pscustomobject]@{ ok = $false; fetched_at = '2026-11-09T13:00:00Z' }) -NowUtc $now
Assert-Same 'SOX　データが不正です' $bad.Text 'unhealthy payload line'
Assert-Same "0.00%" (Format-Percent 0) 'zero percent has no sign'

# アイコンの色と状態
$nowLocal = [DateTime]::new(2026, 11, 9, 22, 20, 0)
$fresh = @($item.PSObject.Copy())
$fresh[0].Stale = $false
function Status([hashtable]$Overrides) {
    $state = @{ GhPath = 'gh.exe'; WorkerFailed = $false; Paused = $false; Phase = 'idle'; ConsecutiveFailures = 0; LastDispatchAt = $nowLocal.AddMinutes(-10); Items = $fresh }
    foreach ($key in $Overrides.Keys) { $state[$key] = $Overrides[$key] }
    Get-MonitorStatus -State $state -NowLocal $nowLocal
}
Assert-Same 'green' (Status @{}).Color 'fresh data is green'
Assert-Same 'red' (Status @{ GhPath = $null }).Color 'missing gh is red'
Assert-Same 'gray' (Status @{ Paused = $true }).Color 'paused is gray'
Assert-Same 'blue' (Status @{ Paused = $true; Phase = 'dispatching' }).Color 'manual run while paused is blue'
Assert-Same 'amber' (Status @{ ConsecutiveFailures = 1 }).Color 'first dispatch failure is amber'
Assert-Same 'red' (Status @{ ConsecutiveFailures = 3 }).Color 'repeated dispatch failure is red'
Assert-Same 'blue' (Status @{ Items = $null }).Color 'no data yet is blue'
Assert-Same 'amber' (Status @{ Items = @($failed) }).Color 'read failure is amber'
Assert-Same 'amber' (Status @{ Items = @($item) }).Color 'stale data is amber'
Assert-Same 'blue' (Status @{ Items = @($item); LastDispatchAt = $nowLocal.AddMinutes(-2) }).Color 'stale right after dispatch is waiting (blue)'

# 前回・次回の表示
Assert-Same '前回の起動 22:15 · 次回 22:30' (Format-Schedule -LastDispatchAt $nowLocal.AddMinutes(-5) -NextDispatchAt $nowLocal.AddMinutes(10) -Paused $false) 'schedule line'
Assert-Same '前回の起動 なし · 一時停止中' (Format-Schedule -LastDispatchAt $null -NextDispatchAt $null -Paused $true) 'paused schedule line'

if ($script:failures) { Write-Output "$($script:failures) failure(s)"; exit 1 }
Write-Output 'all passed'
