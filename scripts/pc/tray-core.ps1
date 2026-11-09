# 常駐アイコンの表示内容を決める処理。画面やネットワークに依存しないため単体で検証できる。
# Windows PowerShell 5.1 で日本語を読めるよう、UTF-8 BOM付きで保存する。

function ConvertTo-UtcTime($Value) {
    if ($null -eq $Value -or [string]$Value -eq '') { return $null }
    if ($Value -is [DateTime]) { return $Value.ToUniversalTime() }
    try {
        return [DateTime]::Parse([string]$Value, [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
    } catch { return $null }
}

function Format-Age([TimeSpan]$Age) {
    if ($Age.TotalMinutes -lt 1) { return 'たった今' }
    if ($Age.TotalMinutes -lt 60) { return ('{0}分前' -f [int][math]::Floor($Age.TotalMinutes)) }
    if ($Age.TotalHours -lt 48) { return ('{0}時間前' -f [int][math]::Floor($Age.TotalHours)) }
    return ('{0}日前' -f [int][math]::Floor($Age.TotalDays))
}

function Format-Percent([double]$Value) {
    $text = [math]::Abs($Value).ToString('0.00', [Globalization.CultureInfo]::InvariantCulture) + '%'
    if ($Value -lt 0) { return [string][char]0x2212 + $text }
    return $text
}

# 公開JSON 1件を、メニューに出す1行と鮮度の判定にまとめる。
function Get-MarketItem {
    param([string]$Name, $Data, [string]$ErrorText, [DateTime]$NowUtc, [double]$MaxAgeHours = 3)
    $fetched = $null
    if ($Data) { $fetched = ConvertTo-UtcTime $Data.fetched_at }
    if ($ErrorText -or -not $Data -or $Data.ok -ne $true -or $null -eq $fetched) {
        $reason = 'データが不正です'
        if ($ErrorText -or -not $Data) { $reason = '読み込めません' }
        return [pscustomobject]@{ Name = $Name; Text = "$Name　$reason"; Stale = $false; Failed = $true; Detail = $ErrorText }
    }
    $age = $NowUtc.ToUniversalTime() - $fetched
    $fetchedText = (Format-Age $age) + 'に取得'
    if ($age.TotalMinutes -lt 1) { $fetchedText = 'たった今取得' }
    if ($null -ne $Data.rolling_drawdown_pct) {
        $day = [DateTime]::ParseExact([string]$Data.current_date, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
        $text = '{0}　{1}（{2}/{3}終値・{4}）' -f $Name, (Format-Percent ([double]$Data.rolling_drawdown_pct)), $day.Month, $day.Day, $fetchedText
    } else {
        $fearGreedJa = @{
            'extreme fear' = '極度の恐怖'; 'fear' = '恐怖'; 'neutral' = '中立'
            'greed' = '強欲'; 'extreme greed' = '極度の強欲'
        }
        $category = [string]$Data.category
        $label = $fearGreedJa[$category.ToLowerInvariant()]
        if (-not $label) { $label = $category }
        $score = ([double]$Data.score).ToString('0.0', [Globalization.CultureInfo]::InvariantCulture)
        $text = '{0}　{1} {2}（{3}）' -f $Name, $score, $label, $fetchedText
    }
    $stale = $age.TotalHours -ge $MaxAgeHours
    if ($stale) { $text += '・古い' }
    return [pscustomobject]@{ Name = $Name; Text = $text; Stale = $stale; Failed = $false; Detail = $null }
}

# アイコンの色と状態の文言を決める。上にある条件ほど優先する。
function Get-MonitorStatus {
    param([hashtable]$State, [DateTime]$NowLocal)
    function New-Status([string]$Key, [string]$Color, [string]$Label) {
        [pscustomobject]@{ Key = $Key; Color = $Color; Label = $Label }
    }
    if (-not $State.GhPath) { return New-Status 'no-gh' 'red' 'GitHub CLI（gh）が見つかりません' }
    if ($State.WorkerFailed) { return New-Status 'worker' 'red' '内部エラー（自動で再開します）' }
    if ($State.Phase -eq 'dispatching') { return New-Status 'dispatching' 'blue' '更新処理を起動しています' }
    if ($State.Paused) { return New-Status 'paused' 'gray' '一時停止中' }
    $failures = [int]$State.ConsecutiveFailures
    if ($failures -ge 3) { return New-Status 'dispatch-failed' 'red' 'GitHubの更新処理を起動できません' }
    if ($failures -ge 1) { return New-Status 'dispatch-retry' 'amber' '起動に失敗したため再試行します' }
    if ($null -eq $State.Items) { return New-Status 'loading' 'blue' 'データを確認しています' }
    $items = @($State.Items)
    if (@($items | Where-Object { $_.Failed }).Count) { return New-Status 'read-failed' 'amber' 'GitHubのデータを読めません' }
    if (@($items | Where-Object { $_.Stale }).Count) {
        if ($State.LastDispatchAt -and ($NowLocal - $State.LastDispatchAt).TotalMinutes -lt 5) {
            return New-Status 'waiting' 'blue' 'GitHubの更新を待っています'
        }
        return New-Status 'stale' 'amber' 'データが古い（ChatGPTは判定を保留）'
    }
    return New-Status 'ok' 'green' '正常に監視中'
}

function Format-Schedule {
    param($LastDispatchAt, $NextDispatchAt, [bool]$Paused)
    $last = '前回の起動 なし'
    if ($LastDispatchAt) { $last = '前回の起動 ' + $LastDispatchAt.ToString('H:mm') }
    if ($Paused) { return $last + ' · 一時停止中' }
    if ($NextDispatchAt) { return $last + ' · 次回 ' + $NextDispatchAt.ToString('H:mm') }
    return $last
}
