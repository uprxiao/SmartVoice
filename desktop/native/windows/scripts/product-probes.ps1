# S4 production helper checks within the existing disposable Medium worker/fixture.
# Dot-sourced by run-probes; no production mock switch or fixture action is exposed by the helper.
$script:ProductResults = [System.Collections.Generic.List[object]]::new()
$productInfo = [System.Diagnostics.ProcessStartInfo]::new((Join-Path $env:CARGO_TARGET_DIR 'release/smartvoiceinput-input.exe'))
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
function Product([string]$Action, [hashtable]$Data = @{}) {
    $script:ProductCallId++
    $request = @{} + $Data
    $request.id = $script:ProductCallId
    $request.action = $Action
    $script:Product.StandardInput.WriteLine(($request | ConvertTo-Json -Compress -Depth 12))
    $script:Product.StandardInput.Flush()
    $read = $script:Product.StandardOutput.ReadLineAsync()
    if (-not $read.Wait(5000) -or $null -eq $read.Result) { throw "Product helper timeout/disconnected: $Action; never replay" }
    $response = $read.Result | ConvertFrom-Json
    $callLog.WriteLine((@{kind='product';request=$request;response=$response} | ConvertTo-Json -Compress -Depth 20))
    $callLog.Flush()
    Assert-Probe ($response.id -eq $request.id) 'Product response id mismatch'
    return $response
}
function Product-Case([string]$Id, [scriptblock]$Body) {
    $started = [DateTime]::UtcNow
    try {
        $observations = & $Body
        $script:ProductResults.Add(@{id=$Id;status='pass';observations=$observations;started_at=$started.ToString('o');ended_at=[DateTime]::UtcNow.ToString('o')})
    } catch {
        $script:ProductResults.Add(@{id=$Id;status='fail';error=$_.Exception.Message;started_at=$started.ToString('o');ended_at=[DateTime]::UtcNow.ToString('o')})
    }
    Write-Host "$Id $($script:ProductResults[-1].status) $($script:ProductResults[-1]['error'])"
}
try {
    Product-Case 'TC-WIN-10-product' {
        $permission = Product 'permission'
        Assert-Probe ($permission.supported -and $permission.trusted -and -not $permission.observationSupported) 'Windows capability mismatch'
        $probe = Product 'probePaste' @{text='must not paste'}
        Assert-Probe ($probe.error.code -eq 'NATIVE_REQUEST_INVALID' -and $probe.error.effect -eq 'none') 'Production exposed probe actions'
        $invalid = Product 'insertCurrent' @{text='x'}
        Assert-Probe ($invalid.error.effect -eq 'none') 'Missing insertion ID was not rejected'
        return @{permission=$permission;probe_rejected=$probe;invalid_rejected=$invalid}
    }
    Product-Case 'TC-WIN-01-product-electron' {
        $groups = @()
        foreach ($fieldName in @('plain','editor')) {
            foreach ($position in @('selection','caret','empty')) {
                $initial = if ($position -eq 'empty') { '' } else { 'BEGIN React END' }
                $start = if ($position -eq 'empty') { 0 } else { 6 }
                $length = if ($position -eq 'selection') { 5 } else { 0 }
                $field = Fixture 'field' @{field=$fieldName;initial=$initial;start=$start;end=($start+$length)}
                Focus-Target $field.pid
                $null = Native 'probeSeedClipboard'
                $text = '中文🙂'
                $result = Product 'insertCurrent' @{insertion_id="electron-$fieldName-$position";text=$text}
                $after = Fixture 'fieldState'
                $expected = $initial.Substring(0,$start) + $text + $initial.Substring($start+$length)
                Assert-Probe ($result.status -eq 'inserted' -and $result.effect -eq 'written' -and $result.observable) 'Production Electron result unverified'
                Assert-Probe ($after.value -ceq $expected -and $after.start -eq ($start+$text.Length) -and $after.end -eq $after.start) 'Independent DOM text/caret mismatch'
                Assert-Probe ($after.chords.Count -eq 1 -and -not $after.chords[0].shift -and -not $after.chords[0].alt -and -not $after.chords[0].meta) 'Product sent more than one ordinary Ctrl+V'
                $clip = Native 'probeClipboardState'
                Assert-Probe ($clip.text -ceq 'SVI original clipboard') 'Product did not restore original clipboard'
                $null = Product 'release' @{target=$result.target}
                $groups += @{field=$fieldName;position=$position;result=$result;independent=$after;clipboard=$clip}
            }
        }
        return @{groups=$groups}
    }
    Product-Case 'TC-WIN-01-product-notepad-chrome' {
        $groups = @()
        foreach ($kind in @('notepad','input','textarea')) {
            if ($kind -eq 'notepad') {
                $file = Join-Path $OutputDir 'product-notepad.txt'
                [System.IO.File]::WriteAllText($file, 'BEGIN React END', $Utf8)
                $target = Start-Owned (Join-Path $env:WINDIR 'System32/notepad.exe') @($file)
            } else {
                $page = [System.Uri]::new((Join-Path $NativeRoot 'fixtures/chrome.html')).AbsoluteUri + '?kind=' + $kind
                $target = Start-Owned $ChromeExe @("--user-data-dir=$(Join-Path $OutputDir "product-$kind")",'--no-first-run','--no-default-browser-check','--disable-sync',"--app=$page")
            }
            Focus-Target $target.Id
            $null = Native 'probeSelect' @{fixture_pid=$target.Id;start=6;length=5}
            $before = Capture-Target $target.Id
            Assert-Probe ($before.readback.text -ceq 'BEGIN React END') 'Product fixture baseline mismatch'
            $null = Native 'probeSeedClipboard'
            $result = Product 'insertCurrent' @{insertion_id="actual-$kind";text='中文🙂'}
            $after = Capture-Target $target.Id
            Assert-Probe ($result.status -eq 'inserted' -and $result.observable -and $after.readback.text -ceq 'BEGIN 中文🙂 END' -and $after.readback.caret_utf16 -eq 10 -and $after.readback.selection_utf16_len -eq 0) 'Product real target did not exactly match independent expectation'
            $groups += @{kind=$kind;result=$result;independent=$after}
        }
        return @{groups=$groups}
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
    Product-Case 'TC-WIN-06-07-product' {
        $groups = @()
        foreach ($name in @('password','readonly','unreadable')) {
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
            $groups += @{field=$name;result=$result;independent=$after}
        }
        return @{groups=$groups}
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
} finally {
    $script:Product.StandardInput.Close()
    if (-not $script:Product.WaitForExit(3000)) { $script:Product.Kill($true); $script:Product.WaitForExit(3000) | Out-Null }
    if ($productStderr.Wait(1000)) { [System.IO.File]::WriteAllText((Join-Path $OutputDir 'product-stderr.txt'), $productStderr.Result, $Utf8) }
    Write-NewJson @{planned=8;cases=@($script:ProductResults);scope='production helper against independent fixtures; not packaged product/audio/S5'} (Join-Path $OutputDir 'product-cases.json')
}
