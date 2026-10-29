# S4 production helper checks within the existing disposable Medium worker/fixture.
# Dot-sourced by run-probes; no production mock switch or fixture action is exposed by the helper.
$script:ProductResults = [ordered]@{}
$plannedIds = @('TC-WIN-10-product','TC-WIN-05-product','TC-WIN-03-product','TC-WIN-09-product','TC-WIN-15-product')
foreach ($f in @('plain','editor')) { foreach ($p in @('selection','caret','empty')) { $plannedIds += "TC-WIN-01-product-electron-$f-$p" } }
foreach ($k in @('notepad','input','textarea')) { $plannedIds += "TC-WIN-01-product-$k" }
foreach ($k in @('notepad','input','textarea')) { foreach ($p in @('caret','empty')) { $plannedIds += "TC-WIN-01-product-$k-$p" } }
foreach ($k in @('password','readonly','unreadable')) { $plannedIds += "TC-WIN-06-07-product-$k" }
foreach ($k in @('unicode','html','rtf','dib','files','gdi-private','oversize','bitmap','dib-conflict')) { $plannedIds += "TC-WIN-13-product-$k" }
foreach ($k in @('ignore','partial','caret','selection')) { $plannedIds += "TC-WIN-04-product-$k" }
foreach ($id in @('TC-WIN-06-product-button','TC-WIN-10-product-invalid-lines','TC-WIN-10-product-eof','TC-WIN-10-product-oversize-line','TC-WIN-10-product-parent-exit','TC-WIN-11-product','TC-WIN-14-16-product')) { $plannedIds += $id }
$plannedIds += 'TC-WIN-08-product-parent-during-wait'
foreach($id in @('TC-WIN-02-product-latest-position','TC-WIN-02-product-latest-window','TC-WIN-03-product-selection','TC-WIN-03-product-control','TC-WIN-03-product-dead-window','TC-WIN-03-product-own-restore','TC-WIN-15-product-slow-uia','TC-WIN-01-product-notepad-crlf','TC-WIN-01-product-electron-lf','TC-WIN-01-product-electron-crlf')) { $plannedIds += $id }
$plannedIds += 'TC-WIN-04-product-late-readback'
$plannedIds += 'TC-WIN-03-product-owned-popup'
$script:SlowUiaEvidence=$null
foreach ($id in $plannedIds) { $script:ProductResults[$id] = @{id=$id;status='not_run';reason='not attempted'} }
$productInfo = [System.Diagnostics.ProcessStartInfo]::new((Join-Path $env:CARGO_TARGET_DIR 'x86_64-pc-windows-msvc/release/smartvoiceinput-input.exe'))
$productInfo.UseShellExecute = $false
$productInfo.CreateNoWindow = $true
$productInfo.RedirectStandardInput = $true
$productInfo.RedirectStandardOutput = $true
$productInfo.RedirectStandardError = $true
$productInfo.StandardInputEncoding = $Utf8
$productInfo.StandardOutputEncoding = $Utf8
$productInfo.ArgumentList.Add('--parent-pid')
$productInfo.ArgumentList.Add([string]$PID)
$script:Product = [System.Diagnostics.Process]::Start($productInfo)
$script:Owned.Add($script:Product)
$productStderr = $script:Product.StandardError.ReadToEndAsync()
$script:ProductCallId = 0
function Product-Begin([string]$Action, [hashtable]$Data = @{}) {
    $script:ProductCallId++
    $request = @{} + $Data
    $request.id = $script:ProductCallId
    $request.action = $Action
    $watch=[System.Diagnostics.Stopwatch]::StartNew()
    $script:Product.StandardInput.WriteLine(($request | ConvertTo-Json -Compress -Depth 12))
    $script:Product.StandardInput.Flush()
    return @{request=$request;read=$script:Product.StandardOutput.ReadLineAsync();watch=$watch}
}
function Product-End([hashtable]$Pending) {
    $read = $Pending.read
    $request = $Pending.request
    $remaining=[Math]::Max(0,5000-$Pending.watch.ElapsedMilliseconds)
    if ($remaining -eq 0 -or -not $read.Wait([int]$remaining) -or $null -eq $read.Result) { throw "Product helper timeout/disconnected: $($request.action); never replay" }
    $response = $read.Result | ConvertFrom-Json
    $callLog.WriteLine((@{kind='product';request=$request;response=$response;elapsed_ms=$Pending.watch.ElapsedMilliseconds} | ConvertTo-Json -Compress -Depth 20))
    $callLog.Flush()
    Assert-Probe ($response.id -eq $request.id) 'Product response id mismatch'
    return $response
}
function Product([string]$Action, [hashtable]$Data = @{}) {
    return Product-End (Product-Begin $Action $Data)
}
function Wait-FixtureConsumption([hashtable]$Pending) {
    # Publication precedes the actual SendInput/final target checks. Observe consumption;
    # a single early DOM read is not evidence that the eventual paste failed.
    $watch=[Diagnostics.Stopwatch]::StartNew();$attempts=0
    while($watch.ElapsedMilliseconds -lt 2000){
        $actual=Fixture 'fieldState';$attempts++
        if($Pending.read.IsCompleted){throw 'BLOCKED: product finished before the clipboard-race precondition could be acquired'}
        if($actual.value -ceq 'BEGIN 中文🙂 END' -and $actual.chords.Count -eq 1){return @{actual=$actual;wait_ms=$watch.ElapsedMilliseconds;attempts=$attempts}}
    }
    throw 'BLOCKED: target consumption was not observed before the bounded clipboard-race preparation ended'
}
function Product-Case([string]$Id, [scriptblock]$Body) {
    $started = [DateTime]::UtcNow
    try {
        $observations = & $Body
        $script:ProductResults[$Id] = @{id=$Id;status='pass';observations=$observations;started_at=$started.ToString('o');ended_at=[DateTime]::UtcNow.ToString('o')}
    } catch {
        $status=if($_.Exception.Message.StartsWith('BLOCKED:')){'blocked'}else{'fail'}
        $script:ProductResults[$Id] = @{id=$Id;status=$status;error=$_.Exception.Message;started_at=$started.ToString('o');ended_at=[DateTime]::UtcNow.ToString('o')}
    }
    Write-Host "$Id $($script:ProductResults[$Id].status) $($script:ProductResults[$Id]['error'])"
}
try {
    Product-Case 'TC-WIN-10-product' {
        $permission = Product 'permission'
        Assert-Probe ($permission.supported -and $permission.trusted -and -not $permission.observationSupported) 'Windows capability mismatch'
        $probe = Product 'probePaste' @{text='must not paste'}
        Assert-Probe ($probe.error.code -eq 'NATIVE_REQUEST_INVALID' -and $probe.error.effect -eq 'none') 'Production exposed probe actions'
        $ownerProbe = Product 'probeOwnPopup'
        Assert-Probe ($ownerProbe.error.code -eq 'NATIVE_REQUEST_INVALID' -and $ownerProbe.error.effect -eq 'none') 'Production exposed fixture owner/style actions'
        $invalid = Product 'insertCurrent' @{text='x'}
        Assert-Probe ($invalid.error.effect -eq 'none') 'Missing insertion ID was not rejected'
        return @{permission=$permission;probe_rejected=$probe;owner_probe_rejected=$ownerProbe;invalid_rejected=$invalid}
    }
    foreach ($fieldName in @('plain','editor')) {
        foreach ($position in @('selection','caret','empty')) {
            Product-Case "TC-WIN-01-product-electron-$fieldName-$position" {
                $initial = if ($position -eq 'empty') { '' } else { 'BEGIN React END' }
                $start = if ($position -eq 'empty') { 0 } else { 6 }
                $length = if ($position -eq 'selection') { 5 } else { 0 }
                $field = Fixture 'field' @{field=$fieldName;initial=$initial;start=$start;end=($start+$length)}
                Focus-Target $field.pid
                $null = Native 'probeSeedClipboard'
                $beforeRaw = Native 'probeReadNow' @{fixture_pid=$field.pid}
                $text = '中文🙂'
                $result = Product 'insertCurrent' @{insertion_id="electron-$fieldName-$position";text=$text}
                $after = Fixture 'fieldState'
                $afterRaw = Native 'probeReadNow' @{fixture_pid=$field.pid}
                Write-NewJson @{field=$fieldName;position=$position;before=$beforeRaw;after=$afterRaw;result=$result;independent=$after} (Join-Path $OutputDir "product-raw-$fieldName-$position.json")
                $expected = $initial.Substring(0,$start) + $text + $initial.Substring($start+$length)
                Assert-Probe ($result.status -eq 'inserted' -and $result.effect -eq 'written' -and $result.observable) 'Production Electron result unverified'
                Assert-Probe ($after.value -ceq $expected -and $after.start -eq ($start+$text.Length) -and $after.end -eq $after.start) 'Independent DOM text/caret mismatch'
                Assert-Probe ($after.chords.Count -eq 1 -and -not $after.chords[0].shift -and -not $after.chords[0].alt -and -not $after.chords[0].meta) 'Product sent more than one ordinary Ctrl+V'
                $clip = Native 'probeClipboardState'
                Assert-Probe ($clip.text -ceq 'SVI original clipboard') 'Product did not restore original clipboard'
                $null = Product 'release' @{target=$result.target}
                return @{field=$fieldName;position=$position;result=$result;independent=$after;clipboard=$clip}
            }
        }
    }
    foreach ($kind in @('notepad','input','textarea')) {
        foreach ($position in @('selection','caret','empty')) {
        $caseId = if ($position -eq 'selection') { "TC-WIN-01-product-$kind" } else { "TC-WIN-01-product-$kind-$position" }
        Product-Case $caseId {
            $initial = if ($position -eq 'empty') { '' } else { 'BEGIN React END' }
            $start = if ($position -eq 'empty') { 0 } else { 6 }
            $length = if ($position -eq 'selection') { 5 } else { 0 }
            if ($kind -eq 'notepad') {
                $file = Join-Path $OutputDir "product-notepad-$position.txt"
                [System.IO.File]::WriteAllText($file, $initial, $Utf8)
                $target = Start-Owned (Join-Path $env:WINDIR 'System32/notepad.exe') @($file)
            } else {
                $page = [System.Uri]::new((Join-Path $NativeRoot 'fixtures/chrome.html')).AbsoluteUri + '?kind=' + $kind
                if ($position -eq 'empty') { $page += '&empty=1' }
                $target = Start-Owned $ChromeExe @("--user-data-dir=$(Join-Path $OutputDir "product-$kind-$position")",'--no-first-run','--no-default-browser-check','--disable-sync',"--app=$page")
            }
            try {
            $preparation = Wait-OwnedWindow $target
            Focus-Target $target.Id
            $null = Native 'probeSelect' @{fixture_pid=$target.Id;start=$start;length=$length}
            $before = Capture-Target $target.Id
            $beforeRaw = Native 'probeReadNow' @{fixture_pid=$target.Id}
            $null = Native 'probeSeedClipboard'
            $text = '中文🙂'
            $expected = $initial.Substring(0,$start) + $text + $initial.Substring($start+$length)
            $result = Product 'insertCurrent' @{insertion_id="actual-$kind-$position";text=$text}
            $after = Capture-Target $target.Id
            Write-NewJson @{kind=$kind;position=$position;preparation=$preparation;before=$before;before_raw=$beforeRaw;expected=$expected;result=$result;after=$after} (Join-Path $OutputDir "product-raw-$kind-$position.json")
            Assert-Probe ($before.readback.text -ceq $initial -and $before.readback.caret_utf16 -eq $start -and $before.readback.selection_utf16_len -eq $length) 'Product fixture baseline text/selection mismatch'
            Assert-Probe ($result.status -eq 'inserted' -and $result.effect -eq 'written' -and $result.observable -and $after.readback.text -ceq $expected -and $after.readback.caret_utf16 -eq ($start+$text.Length) -and $after.readback.selection_utf16_len -eq 0) 'Product real target did not exactly match independent expectation'
            $clipboard = Native 'probeClipboardState'
            Assert-Probe ($clipboard.text -ceq 'SVI original clipboard') 'Product real target did not restore Unicode clipboard'
            $null = Product 'release' @{target=$result.target}
            return @{kind=$kind;position=$position;preparation=$preparation;before=$before;expected=$expected;result=$result;independent=$after;clipboard=$clipboard}
            } finally { Stop-OneOwned $target }
        }
        }
    }
    Product-Case 'TC-WIN-05-product' {
        Focus-Target $HighTargetId
        $null = Native 'probeSeedClipboard'
        $before = Native 'probeClipboardState'
        $result = Product 'insertCurrent' @{insertion_id='higher-il';text='MUST NOT BE WRITTEN'}
        $after = Native 'probeClipboardState'
        Assert-Probe ($result.status -eq 'failed' -and $result.effect -eq 'none' -and $result.code -eq 'PERMISSION_DENIED') 'Product high-IL was not zero-write'
        Assert-Probe ($before.sequence -eq $after.sequence -and $before.text -ceq $after.text) 'Product high-IL changed clipboard'
        return @{result=$result;before=$before;after=$after}
    }
    $highRequestTemporary=Join-Path $OutputDir 'release-high.request.tmp'
    Write-NewJson @{schema='svi-release-owned-high-v1';high_pid=$HighTargetId;after_case='TC-WIN-05-product'} $highRequestTemporary
    [IO.File]::Move($highRequestTemporary,(Join-Path $OutputDir 'release-high.request.json'))
    $highReleaseFile=Join-Path $OutputDir 'release-high.response.json';$highReleaseEnd=[DateTime]::UtcNow.AddSeconds(5)
    while(-not (Test-Path $highReleaseFile)){
        if([DateTime]::UtcNow -ge $highReleaseEnd){throw 'Owned High fixture cleanup was not confirmed; no later foreground preparation attempted'}
        Start-Sleep -Milliseconds 20
    }
    $highRelease=Get-Content -Raw $highReleaseFile|ConvertFrom-Json
    Assert-Probe ($highRelease.schema -eq 'svi-release-owned-high-v1' -and $highRelease.high_pid -eq $HighTargetId -and $highRelease.original_owned_exit_confirmed) 'Owned High fixture cleanup identity differs'
    foreach ($name in @('password','readonly','unreadable')) {
        Product-Case "TC-WIN-06-07-product-$name" {
            $field = Fixture 'field' @{field=$name}
            Focus-Target $field.pid
            $null = Native 'probeSeedClipboard'
            $result = Product 'insertCurrent' @{insertion_id="protected-$name";text='中文🙂'}
            $after = Fixture 'fieldState'
            if ($name -eq 'unreadable') {
                Assert-Probe ($result.status -eq 'unverified' -and $result.effect -eq 'unknown' -and -not $result.observable -and $after.canvasPasteCount -eq 1) 'Product custom target was incorrectly verified/repeated'
            } else {
                Assert-Probe ($result.status -eq 'failed' -and $result.effect -eq 'none' -and $after.value -ceq 'fixture-only') 'Product changed protected field'
            }
            return @{field=$name;result=$result;independent=$after}
        }
    }
    Product-Case 'TC-WIN-03-product' {
        $field = Fixture 'field' @{field='plain'}
        Focus-Target $field.pid
        $capture = Product 'capture'
        $valid = Product 'validate' @{target=$capture.target}
        Assert-Probe $valid.valid 'Fresh explicit target invalid'
        $null = Fixture 'field' @{field='editor'}
        Focus-Target $field.pid
        $invalid = Product 'validate' @{target=$capture.target}
        Assert-Probe (-not $invalid.valid) 'Changed control accepted as explicit target'
        $result = Product 'insert' @{target=$capture.target;insertion_id='explicit-invalid';text='MUST NOT WRITE'}
        Assert-Probe ($result.status -eq 'failed' -and $result.effect -eq 'none') 'Explicit invalid target was written'
        $null = Product 'release' @{target=$capture.target}
        $null = Product 'release' @{target=$capture.target}
        return @{capture=$capture;valid=$valid;changed=$invalid;result=$result}
    }
    Product-Case 'TC-WIN-09-product' {
        $field = Fixture 'field' @{field='plain'}
        Focus-Target $field.pid
        $null = Native 'probeSeedClipboard'
        $one = Product 'insertCurrent' @{insertion_id='duplicate';text='中文🙂'}
        $two = Product 'insertCurrent' @{insertion_id='duplicate';text='中文🙂'}
        $after = Fixture 'fieldState'
        Assert-Probe ($one.status -eq 'inserted' -and $two.status -eq 'unverified' -and $two.effect -eq 'unknown' -and $after.chords.Count -eq 1 -and $after.value -ceq 'BEGIN 中文🙂 END') 'Duplicate production request replayed'
        return @{first=$one;second=$two;independent=$after}
    }
    Product-Case 'TC-WIN-15-product' {
        # Previous product restoration assigns ownership to its own message-only window.
        # Copy from a different helper process while production waits for the next stdin line.
        $watch = [System.Diagnostics.Stopwatch]::StartNew()
        $null = Native 'probeSeedClipboard'
        $clip = Native 'probeClipboardState'
        $watch.Stop()
        Assert-Probe ($watch.ElapsedMilliseconds -lt 3000 -and $clip.text -ceq 'SVI original clipboard') 'External copy blocked by idle product owner'
        return @{external_copy_ms=$watch.ElapsedMilliseconds;clipboard=$clip;scope='independent process, product idle on stdin; UIA-blocked variant remains unexecuted'}
    }
    . (Join-Path $PSScriptRoot 'product-clipboard-probes.ps1')
    . (Join-Path $PSScriptRoot 'product-boundary-probes.ps1')
    . (Join-Path $PSScriptRoot 'product-target-probes.ps1')
} finally {
    $script:Product.StandardInput.Close()
    if (-not $script:Product.WaitForExit(3000)) { $script:Product.Kill($true); $script:Product.WaitForExit(3000) | Out-Null }
    if ($productStderr.Wait(1000)) { [System.IO.File]::WriteAllText((Join-Path $OutputDir 'product-stderr.txt'), $productStderr.Result, $Utf8) }
    Write-NewJson @{planned=$plannedIds.Count;cases=@($script:ProductResults.Values);source_sha=$env:GITHUB_SHA;helper_sha256=(Get-FileHash $productInfo.FileName).Hash;build_profile='x86_64-pc-windows-msvc/+crt-static';scope='formal static CRT helper against independent fixtures; packaged hash checked separately, no audio/S5'} (Join-Path $OutputDir 'product-cases.json')
}
