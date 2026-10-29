param(
    [Parameter(Mandatory=$true)][string]$OutputDir,
    [Parameter(Mandatory=$true)][string]$FixtureExe,
    [Parameter(Mandatory=$true)][string]$ChromeExe,
    [Parameter(Mandatory=$true)][string]$CodeExe,
    [switch]$Worker,
    [int]$HighTargetId = 0
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Verify = Join-Path $env:CARGO_TARGET_DIR 'release/verify.exe'
$Helper = Join-Path $env:CARGO_TARGET_DIR 'release/svi-windows-probe.exe'
$NativeRoot = Split-Path $PSScriptRoot -Parent
$script:Owned = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$script:CallId = 0
$script:FixtureCallId = 0
$script:CleanupConfirmed = $true
$script:Cleanup = [System.Collections.Generic.List[object]]::new()
$script:ChromeReadTraces = [System.Collections.Generic.List[object]]::new()
$Utf8 = [System.Text.UTF8Encoding]::new($false)
. (Join-Path $PSScriptRoot 'probe-files.ps1')
function Assert-Probe([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}
function Start-Owned([string]$Program, [string[]]$Arguments) {
    $info = [System.Diagnostics.ProcessStartInfo]::new($Program)
    $info.UseShellExecute = $false
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    $process = [System.Diagnostics.Process]::Start($info)
    $script:Owned.Add($process)
    return $process
}
function Stop-Owned {
    foreach ($process in $script:Owned) {
        try {
            if (-not $process.HasExited) { $process.Kill($true) }
            if (-not $process.WaitForExit(3000)) { throw "owned process exit not confirmed: $($process.Id)" }
            $script:Cleanup.Add(@{id=$process.Id;exit_confirmed=$true})
        } catch {
            $script:CleanupConfirmed = $false
            $script:Cleanup.Add(@{id=$process.Id;exit_confirmed=$false;error=$_.Exception.Message})
            Write-Warning $_.Exception.Message
        }
    }
}

if (-not $Worker) {
    if (Test-Path $OutputDir) { throw 'OutputDir already exists; choose a new run directory' }
    [System.IO.Directory]::CreateDirectory($OutputDir) | Out-Null
    try {
        $parent = & $Verify --preflight | ConvertFrom-Json
        Write-NewJson $parent (Join-Path $OutputDir 'parent-preflight.json')
        if ($parent.integrity_rid -lt 12288) { throw 'High-integrity parent needed for the UIPI fixture; do not silently omit V-07' }
        Assert-Probe $parent.input_desktop_accessible 'No accessible interactive input desktop'
        $childPreflightFile = Join-Path $OutputDir 'medium-launch-preflight.json'
        & $Verify --medium $Verify --preflight-file $childPreflightFile
        $launchCode = $LASTEXITCODE
        Write-NewJson @{exit_code=$launchCode;preflight_present=(Test-Path $childPreflightFile)} (Join-Path $OutputDir 'medium-launch.json')
        Assert-Probe ($launchCode -eq 0 -and (Test-Path $childPreflightFile)) 'Medium launcher did not complete its independent native preflight'
        $childPreflight = Get-Content -Raw $childPreflightFile | ConvertFrom-Json
        Assert-Probe ($childPreflight.integrity_rid -eq 8192 -and $childPreflight.environment_present.CARGO_TARGET_DIR) 'Medium child lost runtime environment or integrity'
        $highFile = Join-Path $OutputDir 'high-target.txt'
        [System.IO.File]::WriteAllText($highFile, 'HIGH fixture must remain unchanged', $Utf8)
        $high = Start-Owned (Join-Path $env:WINDIR 'System32/notepad.exe') @($highFile)
        $pwsh = (Get-Command pwsh -ErrorAction Stop).Source
        & $Verify --medium $pwsh -NoProfile -File $PSCommandPath -Worker -OutputDir $OutputDir -FixtureExe $FixtureExe -ChromeExe $ChromeExe -CodeExe $CodeExe -HighTargetId $high.Id
        $workerCode = $LASTEXITCODE
        Write-NewJson @{exit_code=$workerCode;summary_present=(Test-Path (Join-Path $OutputDir 'summary.json'))} (Join-Path $OutputDir 'worker-launch.json')
        Assert-Probe (Test-Path (Join-Path $OutputDir 'summary.json')) "Worker did not produce summary (exit $workerCode); keep parent preflight and failure"
        $report = Get-Content -Raw (Join-Path $OutputDir 'summary.json')
        Write-Host $report
        foreach ($item in (Get-Content -Raw (Join-Path $OutputDir 'cases.json') | ConvertFrom-Json -AsHashtable)) {
            Write-Host "$($item.id): $($item.status) $($item.detail['error'])"
        }
        if ($workerCode -ne 0) { throw "Windows technical gate not passed (exit $workerCode); inspect complete report" }
    } catch {
        Write-NewJson @{error=$_.Exception.Message;at=[DateTime]::UtcNow.ToString('o')} (Join-Path $OutputDir 'parent-failure.json')
        $caseFile = Join-Path $OutputDir 'cases.json'
        if (-not (Test-Path $caseFile)) {
            $blocked = @(1..15 | ForEach-Object { @{id=('V-{0:00}' -f $_);status='blocked';detail=@{reason='parent/worker preflight failed before case execution'}} })
            Write-NewJson $blocked $caseFile
        }
        if (-not (Test-Path (Join-Path $OutputDir 'summary.json'))) {
            & $Verify --summarize $caseFile (Join-Path $OutputDir 'summary.json')
        }
        throw
    } finally {
        Stop-Owned
        Write-NewJson @($script:Cleanup) (Join-Path $OutputDir 'parent-cleanup.json')
        if (-not $script:CleanupConfirmed) { throw 'High fixture cleanup not confirmed' }
    }
    exit 0
}

$script:Cases = [ordered]@{}
1..15 | ForEach-Object {
    $id = 'V-{0:00}' -f $_
    $script:Cases[$id] = @{id=$id;status='not_run';detail=@{reason='not attempted in this run'}}
}
$callLog = [System.IO.StreamWriter]::new([System.IO.File]::Open((Join-Path $OutputDir 'native-calls.jsonl'), [System.IO.FileMode]::CreateNew), $Utf8)
$script:Native = $null
$nativeStderr = $null
$fixtureFolder = Join-Path $OutputDir 'fixture-rpc'
[System.IO.Directory]::CreateDirectory($fixtureFolder) | Out-Null

function Native([string]$Action, [hashtable]$Data = @{}) {
    $script:CallId++
    $request = @{} + $Data
    $request.id = $script:CallId
    $request.action = $Action
    $line = $request | ConvertTo-Json -Depth 20 -Compress
    $script:Native.StandardInput.WriteLine($line)
    $script:Native.StandardInput.Flush()
    $task = $script:Native.StandardOutput.ReadLineAsync()
    if (-not $task.Wait(6500)) { throw "Native response timeout ($Action); no write replay" }
    if ($null -eq $task.Result) { throw 'Native process disconnected; no write replay' }
    $response = $task.Result | ConvertFrom-Json
    $callLog.WriteLine((@{request=$request;response=$response} | ConvertTo-Json -Depth 40 -Compress))
    $callLog.Flush()
    if ($response.id -ne $request.id) { throw 'Native response id mismatch' }
    if ($response.PSObject.Properties.Name -contains 'error') { throw "Native probe error: $($response.error.message)" }
    return $response
}
function Fixture([string]$Action, [hashtable]$Data = @{}) {
    $script:FixtureCallId++
    $request = @{} + $Data
    $request.action = $Action
    $prefix = [string]$script:FixtureCallId
    $temp = Join-Path $fixtureFolder "$prefix.request.tmp"
    Write-NewJson $request $temp
    [System.IO.File]::Move($temp, (Join-Path $fixtureFolder "$prefix.request.json"))
    $file = Join-Path $fixtureFolder "$prefix.response.json"
    $end = [DateTime]::UtcNow.AddSeconds(35)
    while (-not (Test-Path $file)) {
        if ([DateTime]::UtcNow -ge $end) { throw "Fixture response timeout ($Action)" }
        Start-Sleep -Milliseconds 40
    }
    $response = Get-Content -Raw $file | ConvertFrom-Json
    if ($response.PSObject.Properties.Name -contains 'error') { throw "Fixture probe error: $($response.error)" }
    return $response
}
function Case([string]$Id, [scriptblock]$Body) {
    $started = [DateTime]::UtcNow
    try {
        $detail = & $Body
        $status = 'pass'
        if ($null -ne $detail -and $detail.ContainsKey('case_status')) { $status = $detail.case_status }
        $script:Cases[$Id] = @{id=$Id;status=$status;detail=@{started_at=$started.ToString('o');ended_at=[DateTime]::UtcNow.ToString('o');observations=$detail}}
    } catch {
        $status = if ($_.Exception.Message.StartsWith('BLOCKED:')) { 'blocked' } else { 'fail' }
        $script:Cases[$Id] = @{id=$Id;status=$status;detail=@{started_at=$started.ToString('o');ended_at=[DateTime]::UtcNow.ToString('o');error=$_.Exception.Message}}
    }
    try { $null = Fixture 'screenshot' @{label=$Id} } catch { $script:Cases[$Id].detail.screenshot_error=$_.Exception.Message }
    Write-NewJson $script:Cases[$Id] (Join-Path $OutputDir "$Id.json")
    Write-Host "$Id $($script:Cases[$Id].status)"
}
function Focus-Target([int]$TargetId) {
    $focus = Native 'probeFocus' @{fixture_pid=$TargetId}
    Assert-Probe $focus.focused 'BLOCKED: fixture could not become foreground'
    Start-Sleep -Milliseconds 250
}
function Capture-Target([int]$TargetId) {
    return Native 'capture' @{fixture_pid=$TargetId}
}
function Paste-Selection([int]$TargetId, [string]$Text, [string]$Initial, [int]$Length, [string]$Expected) {
    Focus-Target $TargetId
    $null = Native 'probeSelect' @{fixture_pid=$TargetId;start=6;length=$Length}
    Start-Sleep -Milliseconds 200
    $before = Capture-Target $TargetId
    if ($before.info.framework_id -eq 'Chrome') {
        $script:ChromeReadTraces.Add(@{target_id=$TargetId;attempts=$before.attempt_details;info=$before.info;readback=$before.readback})
    }
    Assert-Probe ($before.readback.text -ceq $Initial) 'Fixture initial text mismatch'
    Assert-Probe ($before.readback.caret_utf16 -eq 6 -and $before.readback.selection_utf16_len -eq $Length) 'Fixture initial selection mismatch'
    $null = Native 'probeSeedClipboard'
    $result = Native 'probePaste' @{fixture_pid=$TargetId;text=$Text}
    Assert-Probe ($result.status -eq 'inserted' -and $result.observable -eq $true) 'Paste was not exactly verified'
    Assert-Probe ($result.after.text -ceq $Expected -and $result.after.caret_utf16 -eq (6 + $Text.Length) -and $result.after.selection_utf16_len -eq 0) 'Independent expected text/caret mismatch'
    $clip = Native 'probeClipboardState'
    Assert-Probe ($clip.text -ceq 'SVI original clipboard') 'Original clipboard was not restored'
    return @{before=$before;result=$result;clipboard=$clip}
}

try {
    $env:SVI_WINDOWS_PROBE = '1'
    $workerPreflight = & $Verify --preflight | ConvertFrom-Json
    Write-NewJson $workerPreflight (Join-Path $OutputDir 'worker-preflight.json')
    Assert-Probe ($workerPreflight.integrity_rid -eq 8192) 'Worker is not Medium; High success cannot prove ordinary-user insertion'
    Assert-Probe $workerPreflight.input_desktop_accessible 'Worker has no interactive input desktop'
    $info = [System.Diagnostics.ProcessStartInfo]::new($Helper)
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardInputEncoding = $Utf8
    $info.StandardOutputEncoding = $Utf8
    $script:Native = [System.Diagnostics.Process]::Start($info)
    $script:Owned.Add($script:Native)
    $nativeStderr = $script:Native.StandardError.ReadToEndAsync()
    $hello = Native 'ping'
    Assert-Probe ($hello.integrity_rid -eq 8192) 'Native helper is not Medium'
    Write-NewJson $hello (Join-Path $OutputDir 'native-ready.json')
    $fixtureProcess = Start-Owned $FixtureExe @('--probe-dir', $fixtureFolder)
    $readyFile = Join-Path $fixtureFolder 'ready.json'
    $end = [DateTime]::UtcNow.AddSeconds(25)
    while (-not (Test-Path $readyFile)) {
        if ([DateTime]::UtcNow -ge $end) { throw 'Electron packaged fixture not ready' }
        Start-Sleep -Milliseconds 100
    }
    $ready = Get-Content -Raw $readyFile | ConvertFrom-Json
    Assert-Probe ($ready.packaged -eq $true -and $ready.helper_exists -eq $true) 'Fixture was not packaged with its helper'
    $notepadFile = Join-Path $OutputDir 'medium-target.txt'
    [System.IO.File]::WriteAllText($notepadFile, 'BEGIN React END', $Utf8)
    $notepad = Start-Owned (Join-Path $env:WINDIR 'System32/notepad.exe') @($notepadFile)

    Case 'V-01' {
        Focus-Target $notepad.Id
        $before = Native 'foreground'
        $observations = @()
        foreach ($state in @('show', 'update', 'hide')) {
            $hud = Fixture 'hud' @{state=$state}
            foreach ($sample in 1..5) {
                $foreground = Native 'foreground'
                Assert-Probe ($foreground.admission.info.hwnd -eq $before.admission.info.hwnd) 'HUD changed GetForegroundWindow'
                $observations += @{state=$state;sample=$sample;hwnd=$foreground.admission.info.hwnd;hud=$hud}
                Start-Sleep -Milliseconds 60
            }
        }
        return @{samples=$observations;scope='independent Electron window with product HUD options'}
    }
    Case 'V-02' {
        $insert = "中文🙂`r`nDocker"
        $expected = "BEGIN $insert END"
        $observed = Paste-Selection $notepad.Id $insert 'BEGIN React END' 5 $expected
        $caret = Paste-Selection $notepad.Id 'TypeScript' $expected 0 "BEGIN TypeScript$insert END"
        return @{selection=$observed;caret=$caret}
    }
    $script:ChromeObservations = @()
    Case 'V-03' {
        $parameters = @()
        foreach ($kind in @('input','textarea')) {
            try {
            $page = [System.Uri]::new((Join-Path $NativeRoot 'fixtures/chrome.html')).AbsoluteUri + '?kind=' + $kind
            $profile = Join-Path $OutputDir "chrome-$kind"
            $chrome = Start-Owned $ChromeExe @("--user-data-dir=$profile", '--no-first-run', '--no-default-browser-check', '--disable-sync', "--app=$page")
            $insert = if ($kind -eq 'input') { '中文🙂 Docker' } else { "中文🙂`nDocker" }
            $expected = "BEGIN $insert END"
            $observation = Paste-Selection $chrome.Id $insert 'BEGIN React END' 5 $expected
            $caret = Paste-Selection $chrome.Id 'TypeScript' $expected 0 "BEGIN TypeScript$insert END"
            $script:ChromeObservations += @{kind=$kind;observation=$observation;caret=$caret;target_id=$chrome.Id}
            $parameters += @{kind=$kind;status='pass';selection=$observation;caret=$caret}
            } catch {
                $parameters += @{kind=$kind;status='fail';error=$_.Exception.Message}
            }
        }
        Write-NewJson $parameters (Join-Path $OutputDir 'V-03-parameters.json')
        Assert-Probe (@($parameters | Where-Object { $_.status -ne 'pass' }).Count -eq 0) 'Chrome parameter group failed; inspect V-03-parameters.json and native calls'
        return @{cases=$parameters}
    }
    Case 'V-04' {
        Assert-Probe ($env:SVI_MODIFIER_POLICY_CHECKED -eq '1') 'BLOCKED: modifier provenance policy tests were not confirmed before Windows behavior probes'
        $field = Fixture 'field' @{field='editor'}
        Focus-Target $field.pid
        Start-Sleep -Milliseconds 200
        $null = Native 'probeSeedClipboard'
        try {
            $hold = Native 'probeHoldModifiers' @{fixture_pid=$field.pid}
            $result = Native 'probePaste' @{fixture_pid=$field.pid;text='中文🙂'}
        } finally { $released = Native 'probeReleaseModifiers' }
        $actual = Fixture 'fieldState'
        Assert-Probe ($result.status -eq 'inserted') 'Held-modifier paste was not verified'
        Assert-Probe ($actual.chords.Count -eq 1 -and $actual.chords[0].ctrl -and -not $actual.chords[0].shift -and -not $actual.chords[0].alt -and -not $actual.chords[0].meta) 'Target did not receive exactly one plain Ctrl+V'
        Assert-Probe ($result.held_modifiers_after.Count -eq 0) 'Injected modifier remains down'
        Assert-Probe ($hold.held -contains 162 -and $hold.held -contains 160 -and $result.neutralised_modifiers -contains 162 -and $result.neutralised_modifiers -contains 160) 'Both held modifiers were not actually neutralised'
        return @{held=$hold;result=$result;target=$actual;release_cleanup=$released;scope='phase1 injected held state + target event + policy unit tests';policy_tests='modifier_policy::{injected_neutralisation_does_not_erase_physical_provenance,physical_release_before_restore_never_synthesises_that_key_down,startup_state_is_unknown_until_a_noninjected_transition}';physical_keyboard_verified=$false;physical_keyboard_required_at='S5';decision='2026-10-29 owner approved layered verification'}
    }
    Case 'V-06' {
        $cycle = Native 'probeClipboardCycle'
        Assert-Probe ($cycle.restored -and $cycle.restored_text -ceq 'SVI original clipboard') 'Text clipboard restoration did not preserve original'
        Assert-Probe (-not $cycle.replaced_restored -and $cycle.replaced_text -ceq 'SVI intervening clipboard') 'Intervening clipboard version was overwritten'
        return @{cycle=$cycle;case_status='partial';remaining='HTML/RTF/image/file-list fidelity still requires separate verification'}
    }
    Case 'V-07' {
        $medium = Native 'describeWindow' @{fixture_pid=$notepad.Id}
        Focus-Target $HighTargetId
        $higher = Native 'describeWindow' @{fixture_pid=$HighTargetId}
        $result = Native 'probePaste' @{fixture_pid=$HighTargetId;text='MUST NOT BE WRITTEN'}
        Assert-Probe ($result.status -eq 'rejected' -and $result.effect -eq 'none' -and $result.admission.reject -eq 'target_higher_integrity') 'High-integrity foreground was not rejected before writing'
        Assert-Probe ($medium.integrity[0] -eq 'same' -and $higher.integrity[0] -eq 'target_higher') 'Fixture integrity levels mismatch'
        return @{medium=$medium;high=$higher;result=$result}
    }
    Case 'V-08' {
        $observations = @()
        foreach ($fieldName in @('password','readonly','disabled')) {
            $field = Fixture 'field' @{field=$fieldName}
            Focus-Target $field.pid
            Start-Sleep -Milliseconds 200
            $info = Native 'inspectPoint' @{fixture_pid=$field.pid;x=$field.point.x;y=$field.point.y}
            $expected = @{password='password';readonly='read_only';disabled='disabled'}[$fieldName]
            Assert-Probe ($info.class -eq $expected) "UIA did not identify $fieldName"
            $denial = $null
            if ($fieldName -ne 'disabled') {
                $denial = Native 'probePaste' @{fixture_pid=$field.pid;text='MUST NOT BE WRITTEN'}
                Assert-Probe ($denial.status -eq 'rejected' -and $denial.effect -eq 'none' -and $denial.reason -eq $expected) 'Protected field was not rejected for its actual protection reason'
                $unchanged = Fixture 'fieldState'
                Assert-Probe ($unchanged.value -ceq 'fixture-only') 'Protected field was changed'
            }
            $observations += @{field=$fieldName;info=$info;denial=$denial}
        }
        $canvas = Fixture 'field' @{field='unreadable'}
        Focus-Target $canvas.pid
        Start-Sleep -Milliseconds 200
        $null = Native 'probeSeedClipboard'
        $unknown = Native 'probePaste' @{fixture_pid=$canvas.pid;text='中文🙂'}
        $canvasState = Fixture 'fieldState'
        Assert-Probe ($unknown.status -eq 'unverified' -and -not $unknown.observable) 'Unverifiable custom input was incorrectly confirmed'
        Assert-Probe ($canvasState.canvasPasteCount -eq 1 -and $canvasState.canvasValue -ceq '中文🙂') 'Custom fixture did not receive exactly one paste'
        return @{observations=$observations;unknown=$unknown;independent_canvas=$canvasState}
    }
    Case 'V-09' {
        Assert-Probe ($script:ChromeReadTraces.Count -gt 0) 'BLOCKED: Chrome reads did not execute'
        $traces = @($script:ChromeReadTraces)
        $emptyThenReadable = @($traces | Where-Object {
            $_.attempts.Count -gt 1 -and ($null -eq $_.attempts[0].text_utf16_len -or $_.attempts[0].text_utf16_len -eq 0) -and $_.attempts[-1].text_utf16_len -gt 0 -and $_.attempts[-1].has_caret
        })
        if ($emptyThenReadable.Count -gt 0 -and $script:Cases['V-08'].status -eq 'pass') {
            return @{traces=$traces;real_initial_empty_observed=$true;unreadable_fallback='V-08 custom input actual paste remains unverified'}
        }
        return @{traces=$traces;real_initial_empty_observed=$false;case_status='partial';reason='first-empty recovery not observed in this run; do not infer success from already-warmed Chrome'}
    }
    Case 'V-10' {
        $observations = @()
        Focus-Target $notepad.Id
        $observations += @{kind='classic';capture=(Capture-Target $notepad.Id)}
        Assert-Probe ($script:ChromeReadTraces.Count -gt 0) 'BLOCKED: no Chrome target'
        $chromeTarget = $script:ChromeReadTraces[0].target_id
        Focus-Target $chromeTarget
        $observations += @{kind='chrome';capture=(Capture-Target $chromeTarget)}
        $codeFile = Join-Path $OutputDir 'code-fixture.txt'
        [System.IO.File]::WriteAllText($codeFile, 'React Docker TypeScript', $Utf8)
        $codeProfile = Join-Path $OutputDir 'code-profile'
        $code = Start-Owned $CodeExe @('--new-window', '--disable-extensions', "--user-data-dir=$codeProfile", $codeFile)
        Start-Sleep -Seconds 3
        Focus-Target $code.Id
        $observations += @{kind='vscode';capture=(Capture-Target $code.Id)}
        foreach ($observation in $observations) {
            Assert-Probe ($null -ne $observation.capture.caret.rect) "Empty position: $($observation.kind)"
            Assert-Probe ($observation.capture.caret.chosen -ne 'cursor') "Only mouse fallback available: $($observation.kind)"
        }
        return @{observations=$observations;multiscreen_dpi_verified=$false}
    }
    Case 'V-13' {
        $registrations = Fixture 'hotkeys'
        return @{registration=$registrations;case_status='partial';reason='registration only; real Chinese IME response not tested'}
    }
    Case 'V-14' {
        $result = Fixture 'helpers'
        Assert-Probe ($result.packaged -and $result.helper_path_verified -and $result.attempts.Count -eq 4) 'Packaged helper path/start/restart not verified'
        Assert-Probe ($result.attempts[2].pid -ne $result.attempts[3].pid) 'Crash restart reused old PID'
        return @{result=$result;scope='independent packaged Electron fixture, not product integration'}
    }
    . (Join-Path $PSScriptRoot 'product-probes.ps1')
} catch {
    Write-NewJson @{error=$_.Exception.Message;at=[DateTime]::UtcNow.ToString('o')} (Join-Path $OutputDir 'worker-failure.json')
} finally {
    if ($null -ne $script:Native -and -not $script:Native.HasExited) {
        try { $null = Native 'probeReleaseModifiers' } catch { Write-Warning 'Could not confirm injected modifier cleanup' }
        $script:Native.StandardInput.Close()
    }
    try { $null = Fixture 'quit' } catch { Write-Warning 'Fixture quit not confirmed through RPC' }
    Stop-Owned
    $callLog.Dispose()
    Write-NewJson @($script:Cleanup) (Join-Path $OutputDir 'worker-cleanup.json')
    if (-not $script:CleanupConfirmed -and $script:Cases['V-14'].status -eq 'pass') {
        $script:Cases['V-14'].status = 'fail'
        $script:Cases['V-14'].detail.cleanup_error = 'owned process termination not confirmed'
    }
    if ($null -ne $nativeStderr -and $nativeStderr.Wait(1000)) {
        [System.IO.File]::WriteAllText((Join-Path $OutputDir 'helper-stderr.txt'), $nativeStderr.Result, $Utf8)
    }
    $casesFile = Join-Path $OutputDir 'cases.json'
    Write-NewJson @($script:Cases.Values) $casesFile
    & $Verify --summarize $casesFile (Join-Path $OutputDir 'summary.json')
}
$summary = Get-Content -Raw (Join-Path $OutputDir 'summary.json') | ConvertFrom-Json
if (-not $summary.technical_gate_passed) { exit 1 }
$products = Get-Content -Raw (Join-Path $OutputDir 'product-cases.json') | ConvertFrom-Json
if ($products.cases.Count -ne $products.planned -or @($products.cases | Where-Object status -ne 'pass').Count -gt 0) { exit 1 }
exit 0
