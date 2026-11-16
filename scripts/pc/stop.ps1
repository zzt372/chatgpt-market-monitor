# Ask the running tray icon to exit (it removes its icon cleanly).
$event = $null
if ([Threading.EventWaitHandle]::TryOpenExisting('Local\ChatGPTMarketMonitor.Tray.Exit', [ref]$event)) {
    [void]$event.Set()
    $event.Dispose()
    Write-Output 'Stop requested.'
} else {
    Write-Output 'The tray icon is not running.'
}
