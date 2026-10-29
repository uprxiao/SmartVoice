# Target races and multiline cases use actual production protocol and independently owned windows.
Product-Case 'TC-WIN-02-product-latest-position' {
    $field=Fixture 'field' @{field='plain'}
    Focus-Target $field.pid
    $old=Product 'capture'
    $null=Native 'probeSeedClipboard'
    try {
        $held=Native 'probeHoldModifiers' @{fixture_pid=$field.pid}
        $pending=Product-Begin 'insertCurrent' @{insertion_id='latest-position';text='中文🙂'}
        $changed=Fixture 'field' @{field='plain';start=0;end=0}
        $result=Product-End $pending
    } finally {$released=Native 'probeReleaseModifiers'}
    $actual=Fixture 'fieldState'
    Assert-Probe ($held.held -contains 162 -and $held.held -contains 160 -and $result.status -eq 'inserted' -and $actual.value -ceq '中文🙂BEGIN React END' -and $actual.start -eq 4 -and $actual.end -eq 4 -and $actual.chords.Count -eq 1 -and $actual.enterCount -eq 0) 'Automatic insertion used the earlier captured position'
    Assert-Probe (-not $actual.chords[0].shift -and -not $actual.chords[0].alt -and -not $actual.chords[0].meta) 'Latest-position paste was not ordinary Ctrl+V'
    $null=Product 'release' @{target=$old.target};$null=Product 'release' @{target=$result.target}
    return @{old=$old;held=$held;result=$result;actual=$actual;released=$released}
}
Product-Case 'TC-WIN-02-product-latest-window' {
    $targets=@()
    try {
        $file=Join-Path $OutputDir 'product-latest-C.txt'
        [IO.File]::WriteAllText($file,'BEGIN React END',$Utf8)
        $c=Start-Owned (Join-Path $env:WINDIR 'System32/notepad.exe') @($file);$targets+=@($c)
        $null=Wait-OwnedWindow $c;Focus-Target $c.Id
        $null=Native 'probeSelect' @{fixture_pid=$c.Id;start=0;length=0}
        $cBefore=Capture-Target $c.Id
        $page=[Uri]::new((Join-Path $NativeRoot 'fixtures/chrome.html')).AbsoluteUri+'?kind=input'
        $b=Start-Owned $ChromeExe @("--user-data-dir=$(Join-Path $OutputDir 'product-latest-B')",'--no-first-run','--disable-sync',"--app=$page");$targets+=@($b)
        $null=Wait-OwnedWindow $b;Focus-Target $b.Id
        $bBefore=Capture-Target $b.Id
        $a=Fixture 'field' @{field='plain'};Focus-Target $a.pid
        $old=Product 'capture'
        $null=Native 'probeSeedClipboard'
        try {
            $held=Native 'probeHoldModifiers' @{fixture_pid=$a.pid}
            $pending=Product-Begin 'insertCurrent' @{insertion_id='latest-window-C';text='中文🙂'}
            $watch=[Diagnostics.Stopwatch]::StartNew()
            $middle=Native 'probeFocus' @{fixture_pid=$b.Id}
            $last=Native 'probeFocus' @{fixture_pid=$c.Id}
            $movedMs=$watch.ElapsedMilliseconds
            $result=Product-End $pending
        } finally {$released=Native 'probeReleaseModifiers'}
        $cAfter=Capture-Target $c.Id
        $aAfter=Fixture 'fieldState'
        Focus-Target $b.Id;$bAfter=Capture-Target $b.Id
        Write-NewJson @{a=$aAfter;b_before=$bBefore;b_after=$bAfter;c_before=$cBefore;c_after=$cAfter;middle=$middle;last=$last;moved_ms=$movedMs;held=$held;result=$result} (Join-Path $OutputDir 'product-latest-window.json')
        Assert-Probe ($held.held.Count -eq 2 -and $middle.focused -and $last.focused -and $movedMs -lt 450) 'Three-window transition did not finish during the modifier wait'
        Assert-Probe ($result.status -eq 'inserted' -and $cBefore.readback.text -ceq 'BEGIN React END' -and $cBefore.readback.caret_utf16 -eq 0 -and $cAfter.readback.text -ceq '中文🙂BEGIN React END' -and $cAfter.readback.caret_utf16 -eq 4 -and $cAfter.readback.selection_utf16_len -eq 0) 'Automatic insertion did not use final C window'
        Assert-Probe ($aAfter.fields.plain.value -ceq 'BEGIN React END' -and $aAfter.chords.Count -eq 0 -and $bAfter.readback.text -ceq $bBefore.readback.text) 'Earlier A/B target was modified'
        $null=Product 'release' @{target=$old.target};$null=Product 'release' @{target=$result.target}
        return @{result=$result;moved_ms=$movedMs;c=$cAfter;a=$aAfter;b=$bAfter;released=$released}
    } finally {foreach($target in $targets){Stop-OneOwned $target}}
}
foreach($change in @('selection','control')) {
    Product-Case "TC-WIN-03-product-$change" {
        $field=Fixture 'field' @{field='plain'};Focus-Target $field.pid
        $capture=Product 'capture'
        $fieldName=if($change -eq 'control'){'editor'}else{'plain'}
        $null=Fixture 'field' @{field=$fieldName;start=0;end=0}
        $null=Native 'probeSeedClipboard';$before=Native 'probeClipboardState'
        $validation=Product 'validate' @{target=$capture.target}
        $result=Product 'insert' @{target=$capture.target;insertion_id="changed-$change";text='MUST NOT PASTE'}
        $actual=Fixture 'fieldState';$after=Native 'probeClipboardState'
        Assert-Probe (-not $validation.valid -and $result.status -eq 'failed' -and $result.effect -eq 'none' -and $actual.chords.Count -eq 0 -and $actual.value -ceq 'BEGIN React END' -and $before.sequence -eq $after.sequence) 'Explicit history target change was not refused before effects'
        $one=Product 'release' @{target=$capture.target};$two=Product 'release' @{target=$capture.target}
        Assert-Probe ($one.released -and $two.released) 'Repeated target release was not safe'
        return @{change=$change;validation=$validation;result=$result;actual=$actual;release_once=$one;release_twice=$two}
    }
}
Product-Case 'TC-WIN-03-product-dead-window' {
    $file=Join-Path $OutputDir 'product-dead-window.txt';[IO.File]::WriteAllText($file,'BEGIN React END',$Utf8)
    $target=Start-Owned (Join-Path $env:WINDIR 'System32/notepad.exe') @($file)
    try {
        $null=Wait-OwnedWindow $target;Focus-Target $target.Id
        $capture=Product 'capture'
        Stop-OneOwned $target
        $field=Fixture 'field' @{field='plain'};Focus-Target $field.pid
        $null=Native 'probeSeedClipboard';$before=Native 'probeClipboardState'
        $result=Product 'insert' @{target=$capture.target;restoreFocus=$true;insertion_id='dead-window';text='MUST NOT PASTE'}
        $after=Native 'probeClipboardState';$actual=Fixture 'fieldState'
        Assert-Probe ($result.status -eq 'failed' -and $result.effect -eq 'none' -and $actual.chords.Count -eq 0 -and $actual.value -ceq 'BEGIN React END' -and $before.sequence -eq $after.sequence) 'Dead explicit window was rebound to another target'
        $null=Product 'release' @{target=$capture.target}
        return @{result=$result;actual=$actual;before=$before;after=$after;pid_exit_confirmed=$target.HasExited;handle_reuse_verified=$false}
    } finally {if(-not $target.HasExited){Stop-OneOwned $target}}
}
function Peer-Product([int]$Id,[string]$Action,[hashtable]$Data=@{}) {
    $request=@{}+$Data;$request.id=$Id;$request.action=$Action
    $response=Fixture 'productPeerRequest' @{request=$request}
    $callLog.WriteLine((@{kind='production-peer';request=$request;response=$response}|ConvertTo-Json -Depth 20 -Compress));$callLog.Flush()
    Assert-Probe ($response.id -eq $Id) 'Peer production id mismatch'
    return $response
}
Product-Case 'TC-WIN-03-product-own-restore' {
    $field=Fixture 'field' @{field='plain'};Focus-Target $field.pid
    $peer=Fixture 'productPeerStart'
    $target=$null
    try {
        $actualParent=Get-CimInstance Win32_Process -Filter "ProcessId = $($peer.pid)"
        Assert-Probe ($actualParent.ParentProcessId -eq $field.pid -and [IO.Path]::GetFullPath($actualParent.ExecutablePath) -ieq [IO.Path]::GetFullPath($productInfo.FileName)) 'Restore fixture must be the actual OS parent of the production helper'
        $null=Peer-Product 1 'permission'
        $file=Join-Path $OutputDir 'product-restore-window.txt';[IO.File]::WriteAllText($file,'BEGIN React END',$Utf8)
        $target=Start-Owned (Join-Path $env:WINDIR 'System32/notepad.exe') @($file)
        $null=Wait-OwnedWindow $target;Focus-Target $target.Id
        $null=Native 'probeSelect' @{fixture_pid=$target.Id;start=6;length=5}
        $capture=Peer-Product 2 'capture'
        $restoreControl=Native 'probeRestoreControl' @{fixture_pid=$target.Id;fixture_parent_pid=$field.pid}
        Write-NewJson $restoreControl (Join-Path $OutputDir 'product-restore-control.json')
        Focus-Target $field.pid
        $null=Native 'probeSeedClipboard';$before=Native 'probeClipboardState'
        $own=Peer-Product 3 'insertCurrent' @{insertion_id='own-window-refusal';text='MUST NOT PASTE'}
        $ownActual=Fixture 'fieldState';$ownAfter=Native 'probeClipboardState'
        Assert-Probe ($own.status -eq 'failed' -and $own.effect -eq 'none' -and $ownActual.chords.Count -eq 0 -and $before.sequence -eq $ownAfter.sequence) 'Product admitted its own parent window'
        $result=Peer-Product 4 'insert' @{target=$capture.target;restoreFocus=$true;insertion_id='valid-own-restore';text='中文🙂'}
        $actual=Capture-Target $target.Id
        Write-NewJson @{peer=$peer;capture=$capture;control=$restoreControl;own_refusal=$own;result=$result;actual=$actual} (Join-Path $OutputDir 'product-own-restore.json')
        Assert-Probe ($result.status -eq 'inserted' -and $actual.readback.text -ceq 'BEGIN 中文🙂 END' -and $actual.readback.caret_utf16 -eq 10 -and $actual.readback.selection_utf16_len -eq 0) 'Explicit valid target did not restore from the same parent UI'
        $releaseOne=Peer-Product 5 'release' @{target=$capture.target};$releaseTwo=Peer-Product 6 'release' @{target=$capture.target}
        Assert-Probe ($releaseOne.released -and $releaseTwo.released) 'Peer target release was not idempotent'
        return @{own_refusal=$own;own_actual=$ownActual;result=$result;actual=$actual;peer=$peer;actual_parent_pid=$actualParent.ParentProcessId;scope='actual OS parent is the independent fixture; production restoreFocus path, not a Main IPC mock'}
    } finally {
        $peerStop=Fixture 'productPeerStop';Assert-Probe $peerStop.exit_confirmed 'Peer helper exit not confirmed'
        if($null -ne $target -and -not $target.HasExited){Stop-OneOwned $target}
    }
}
Product-Case 'TC-WIN-15-product-slow-uia' {
    $field=Fixture 'field' @{field='plain'};Focus-Target $field.pid
    $null=Native 'probeSeedClipboard'
    $pending=Product-Begin 'insertCurrent' @{insertion_id='slow-uia-clipboard';text='中文🙂'}
    $publication=$null;$watch=[Diagnostics.Stopwatch]::StartNew()
    while($watch.ElapsedMilliseconds -lt 1000){$state=Native 'probeClipboardState';if($state.text -ceq '中文🙂'){$publication=$state;break};if($pending.read.IsCompleted){break}}
    try {
        Assert-Probe ($null -ne $publication) 'Slow UIA publication was not observed'
        $consumed=Fixture 'fieldState'
        Assert-Probe ($consumed.value -ceq 'BEGIN 中文🙂 END' -and $consumed.chords.Count -eq 1) 'Slow UIA target did not first consume text'
        $block=Fixture 'blockMain' @{label='product-slow-uia';duration_ms=2000}
        $started=Join-Path $fixtureFolder 'product-slow-uia-started.json'
        $ended=Join-Path $fixtureFolder 'product-slow-uia-ended.json'
        $limit=[DateTime]::UtcNow.AddSeconds(1)
        while(-not (Test-Path $started)){if([DateTime]::UtcNow -ge $limit){throw 'UIA provider block did not start'};Start-Sleep -Milliseconds 10}
        Start-Sleep -Milliseconds 550
        Assert-Probe (-not $pending.read.IsCompleted -and -not (Test-Path $ended)) 'Actual UIA blocked interval not observed; do not claim slow-provider coverage'
        $copyWatch=[Diagnostics.Stopwatch]::StartNew()
        $external=Native 'probeSeedClipboard' @{kind='intervening'}
        $copyMs=$copyWatch.ElapsedMilliseconds
        Assert-Probe ($copyMs -lt 1000 -and -not (Test-Path $ended)) 'External copy could not finish while production UIA was blocked'
    } finally {$result=Product-End $pending}
    $final=Native 'probeClipboardState';$actual=Fixture 'fieldState'
    Assert-Probe (-not $result.clipboardRestored -and $final.sequence -eq $external.sequence -and $final.text -ceq 'SVI intervening clipboard' -and $actual.value -ceq 'BEGIN 中文🙂 END' -and $actual.chords.Count -eq 1 -and $actual.enterCount -eq 0) 'Slow UIA clipboard handling changed external data or repeated paste'
    Write-NewJson @{publication=$publication;block=$block;copy_ms=$copyMs;elapsed_ms=$watch.ElapsedMilliseconds;result=$result;external=$external;final=$final;actual=$actual} (Join-Path $OutputDir 'product-slow-uia.json')
    $script:SlowUiaEvidence=@{result=$result;elapsed_ms=$watch.ElapsedMilliseconds;provider_block_ms=$block.duration_ms;copy_ms=$copyMs;actual=$actual}
    return @{result=$result;copy_ms=$copyMs;actual=$actual;final=$final;scope='independent fixture main/UIA thread blocked after actual paste; clipboard owner must keep pumping'}
}
Product-Case 'TC-WIN-04-product-late-readback' {
    if($null -eq $script:SlowUiaEvidence){throw 'BLOCKED: actual slow UIA interval was not obtained'}
    $evidence=$script:SlowUiaEvidence
    Assert-Probe ($evidence.provider_block_ms -eq 2000 -and $evidence.elapsed_ms -gt 1350) 'Actual readback did not exceed the frozen 350ms+1000ms budget'
    Assert-Probe ($evidence.result.status -eq 'unverified' -and $evidence.result.effect -eq 'unknown' -and -not $evidence.result.observable) 'Late UIA readback was accepted after the 1000ms verification budget'
    return $evidence
}
foreach($lineEnding in @('lf','crlf')) {
    Product-Case "TC-WIN-01-product-electron-$lineEnding" {
        $field=Fixture 'field' @{field='editor'};Focus-Target $field.pid
        $null=Native 'probeSeedClipboard'
        $text=if($lineEnding -eq 'lf'){"中文🙂`nDocker"}else{"中文🙂`r`nDocker"}
        $result=Product 'insertCurrent' @{insertion_id="multiline-electron-$lineEnding";text=$text}
        $actual=Fixture 'fieldState'
        $expectedStatus=if($lineEnding -eq 'lf'){'inserted'}else{'unverified'}
        Assert-Probe ($result.status -eq $expectedStatus -and $actual.value -ceq "BEGIN 中文🙂`nDocker END" -and $actual.start -eq 17 -and $actual.end -eq 17 -and $actual.chords.Count -eq 1 -and $actual.enterCount -eq 0) 'Raw multiline/caret contract mismatch; never normalize CRLF to fake strict success'
        if($lineEnding -eq 'crlf'){Assert-Probe ($result.effect -eq 'unknown' -and -not $result.observable) 'Transformed CRLF was falsely verified'}else{$null=Product 'release' @{target=$result.target}}
        return @{line_ending=$lineEnding;expected_status=$expectedStatus;result=$result;actual=$actual}
    }
}
Product-Case 'TC-WIN-01-product-notepad-crlf' {
    $file=Join-Path $OutputDir 'product-crlf.txt';[IO.File]::WriteAllText($file,'BEGIN React END',$Utf8)
    $target=Start-Owned (Join-Path $env:WINDIR 'System32/notepad.exe') @($file)
    try {
        $null=Wait-OwnedWindow $target;Focus-Target $target.Id
        $null=Native 'probeSelect' @{fixture_pid=$target.Id;start=6;length=5}
        $null=Native 'probeSeedClipboard'
        $result=Product 'insertCurrent' @{insertion_id='multiline-notepad-crlf';text="中文🙂`r`nDocker"}
        $actual=Capture-Target $target.Id
        Assert-Probe ($result.status -eq 'inserted' -and $actual.readback.text -ceq "BEGIN 中文🙂`r`nDocker END" -and $actual.readback.caret_utf16 -eq 18 -and $actual.readback.selection_utf16_len -eq 0) 'Classic Edit CRLF exact verification failed'
        $null=Product 'release' @{target=$result.target}
        return @{result=$result;actual=$actual}
    } finally {Stop-OneOwned $target}
}
