# Actual production helper against target event behavior, malformed bytes and process lifecycle.
foreach ($mode in @('ignore','partial','caret','selection')) {
    Product-Case "TC-WIN-04-product-$mode" {
        $field = Fixture 'field' @{field='plain';pasteMode=$mode}
        Focus-Target $field.pid
        $null = Native 'probeSeedClipboard'
        $result = Product 'insertCurrent' @{insertion_id="readback-$mode";text='中文🙂'}
        $actual = Fixture 'fieldState'
        $expectedText = switch ($mode) { 'ignore' {'BEGIN React END'} 'partial' {'BEGIN 中 END'} default {'BEGIN 中文🙂 END'} }
        $expectedStart = switch ($mode) { 'ignore' {6} 'partial' {7} default {0} }
        $expectedEnd = switch ($mode) { 'ignore' {11} 'partial' {7} 'selection' {1} default {0} }
        Write-NewJson @{mode=$mode;result=$result;actual=$actual;expected_text=$expectedText;expected_start=$expectedStart;expected_end=$expectedEnd} (Join-Path $OutputDir "product-readback-$mode.json")
        Assert-Probe ($result.status -eq 'unverified' -and $result.effect -eq 'unknown' -and -not $result.observable) 'Incorrect readback was reported as inserted'
        Assert-Probe ($actual.value -ceq $expectedText -and $actual.start -eq $expectedStart -and $actual.end -eq $expectedEnd -and $actual.chords.Count -eq 1 -and $actual.enterCount -eq 0) 'Target did not realize the independent negative scenario exactly once'
        $clip = Native 'probeClipboardState'
        Assert-Probe ($clip.text -ceq 'SVI original clipboard') 'Negative readback did not restore clipboard'
        return @{mode=$mode;result=$result;actual=$actual;clipboard=$clip}
    }
}
Product-Case 'TC-WIN-06-product-button' {
    $field = Fixture 'field' @{field='button'}
    Focus-Target $field.pid
    $null = Native 'probeSeedClipboard'
    $before = Native 'probeClipboardState'
    $result = Product 'insertCurrent' @{insertion_id='button-refusal';text='MUST NOT PASTE'}
    $after = Native 'probeClipboardState'
    $actual = Fixture 'fieldState'
    Assert-Probe ($result.status -eq 'failed' -and $result.effect -eq 'none' -and $before.sequence -eq $after.sequence -and $actual.chords.Count -eq 0 -and $actual.enterCount -eq 0) 'Known non-input received an action or clipboard write'
    return @{result=$result;actual=$actual;before=$before;after=$after}
}
Product-Case 'TC-WIN-11-product' {
    $field = Fixture 'field' @{field='plain'}
    Focus-Target $field.pid
    $null = Native 'probeSeedClipboard'
    try {
        $held = Native 'probeHoldModifiers' @{fixture_pid=$field.pid}
        Assert-Probe ($held.held -contains 162 -and $held.held -contains 160) 'Both actual injected modifiers were not held'
        $result = Product 'insertCurrent' @{insertion_id='held-modifiers';text='中文🙂'}
        $actual = Fixture 'fieldState'
    } finally { $released = Native 'probeReleaseModifiers' }
    Assert-Probe ($result.status -eq 'inserted' -and $actual.value -ceq 'BEGIN 中文🙂 END' -and $actual.start -eq 10 -and $actual.end -eq 10) 'Product held-modifier input was not exactly verified'
    Assert-Probe ($actual.chords.Count -eq 1 -and $actual.chords[0].ctrl -and -not $actual.chords[0].shift -and -not $actual.chords[0].alt -and -not $actual.chords[0].meta -and $actual.enterCount -eq 0 -and $released.held.Count -eq 0) 'Product left a modifier or delivered an extra chord'
    $null = Product 'release' @{target=$result.target}
    return @{held=$held;result=$result;actual=$actual;released=$released;physical_keyboard_verified=$false}
}
Product-Case 'TC-WIN-14-16-product' {
    $field = Fixture 'field' @{field='plain'}
    Focus-Target $field.pid
    $null = Native 'probeSeedClipboard'
    $pending = Product-Begin 'insertCurrent' @{insertion_id='external-clipboard-copy';text='中文🙂'}
    $published = $null
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    while ($watch.ElapsedMilliseconds -lt 2000) {
        $state = Native 'probeClipboardState'
        if ($state.text -ceq '中文🙂') { $published = $state; break }
        if ($pending.read.IsCompleted) { break }
    }
    try {
        Assert-Probe ($null -ne $published) 'Did not observe the actual production clipboard publication'
        $actual = Fixture 'fieldState'
        Assert-Probe ($actual.value -ceq 'BEGIN 中文🙂 END' -and $actual.chords.Count -eq 1) 'Target had not yet consumed the published text'
        # Another process copies after consumption, before production's 350ms readback/restore.
        $external = Native 'probeSeedClipboard' @{kind='intervening'}
    } finally { $result = Product-End $pending }
    $final = Native 'probeClipboardState'
    Write-NewJson @{published=$published;external=$external;result=$result;actual=$actual;final=$final;observed_ms=$watch.ElapsedMilliseconds} (Join-Path $OutputDir 'product-clipboard-external.json')
    foreach ($name in @('ExcludeClipboardContentFromMonitorProcessing','CanIncludeInClipboardHistory','CanUploadToCloudClipboard')) {
        $format = @($published.formats | Where-Object name -ceq $name)
        $value = @($published.exclusions | Where-Object name -ceq $name)
        Assert-Probe ($format.Count -eq 1 -and $format[0].bytes -ge 4 -and $value.Count -eq 1 -and $value[0].dword -eq 0) "Production exclusion format missing/not zero: $name"
    }
    Assert-Probe ($result.status -eq 'inserted' -and -not $result.clipboardRestored -and $final.text -ceq 'SVI intervening clipboard' -and $final.sequence -eq $external.sequence) 'Product overwrote another process clipboard version'
    $null = Product 'release' @{target=$result.target}
    return @{result=$result;published=$published;external=$external;final=$final;cloud_history_verified=$false}
}
Product-Case 'TC-WIN-10-product-invalid-lines' {
    $field = Fixture 'field' @{field='plain'}
    Focus-Target $field.pid
    $null = Native 'probeSeedClipboard'
    $before = Native 'probeClipboardState'
    $rows = @()
    $bad = @(
        '{', '{"id":0,"action":"permission"}', '{"id":-1,"action":"permission"}',
        '{"id":1,"action":"observe"}', '{"id":1,"action":"permission","data":{}}',
        '{"id":1,"action":"insertCurrent","insertion_id":"invalid-empty","text":""}',
        '{"id":1,"action":"insertCurrent","insertion_id":"invalid-type","text":true}',
        '{"id":1,"action":"insertCurrent","insertion_id":"invalid-null","text":"A\u0000B"}',
        '{"id":1,"action":"insertCurrent","insertion_id":"invalid-restore","text":"x","restoreFocus":"yes"}',
        ('{"id":1,"action":"insertCurrent","insertion_id":"invalid-size","text":"' + ('🙂' * 16001) + '"}')
    )
    foreach ($line in $bad) {
        $script:Product.StandardInput.WriteLine($line)
        $script:Product.StandardInput.Flush()
        $read = $script:Product.StandardOutput.ReadLineAsync()
        Assert-Probe ($read.Wait(5000) -and $null -ne $read.Result) 'Invalid protocol did not return a bounded response'
        $response = $read.Result | ConvertFrom-Json
        $rows += @{input_utf16=$line.Length;response=$response}
        Assert-Probe ($response.error.code -eq 'NATIVE_REQUEST_INVALID' -and $response.error.effect -eq 'none' -and -not ($response.PSObject.Properties.Name -contains 'ok') -and -not ($response.PSObject.Properties.Name -contains 'data')) 'Invalid request reached dispatch or changed flat protocol'
    }
    $script:Product.StandardInput.BaseStream.Write([byte[]]@(255,254,10),0,3)
    $script:Product.StandardInput.BaseStream.Flush()
    $read = $script:Product.StandardOutput.ReadLineAsync()
    Assert-Probe ($read.Wait(5000) -and $null -ne $read.Result) 'Invalid UTF-8 did not return a bounded response'
    $utf8 = $read.Result | ConvertFrom-Json
    Assert-Probe ($utf8.id -eq 0 -and $utf8.error.code -eq 'NATIVE_REQUEST_INVALID' -and $utf8.error.effect -eq 'none') 'Invalid UTF-8 was accepted'
    $permission = Product 'permission'
    $after = Native 'probeClipboardState'
    $actual = Fixture 'fieldState'
    Assert-Probe ($permission.supported -and $before.sequence -eq $after.sequence -and $actual.chords.Count -eq 0 -and $actual.enterCount -eq 0 -and $actual.value -ceq 'BEGIN React END') 'Invalid input changed target/clipboard or poisoned subsequent protocol'
    return @{cases=$rows;utf8=$utf8;continued=$permission;after=$after;actual=$actual}
}
foreach ($ending in @('eof','oversize-line','parent-exit')) {
    Product-Case "TC-WIN-10-product-$ending" {
        $logicalParent = $null
        $info = [System.Diagnostics.ProcessStartInfo]::new($productInfo.FileName)
        $info.UseShellExecute=$false; $info.CreateNoWindow=$true
        $info.RedirectStandardInput=$true; $info.RedirectStandardOutput=$true; $info.RedirectStandardError=$true
        $info.StandardInputEncoding=$Utf8; $info.StandardOutputEncoding=$Utf8
        $parentId = $PID
        if ($ending -eq 'parent-exit') {
            $logicalParent = Start-Owned (Get-Command pwsh).Source @('-NoProfile','-Command','Start-Sleep -Seconds 60')
            $parentId = $logicalParent.Id
        }
        $info.ArgumentList.Add('--parent-pid'); $info.ArgumentList.Add([string]$parentId)
        $child = [System.Diagnostics.Process]::Start($info); $script:Owned.Add($child)
        $stderr = $child.StandardError.ReadToEndAsync()
        try {
            $child.StandardInput.WriteLine('{"id":1,"action":"permission"}'); $child.StandardInput.Flush()
            $ready = $child.StandardOutput.ReadLineAsync()
            Assert-Probe ($ready.Wait(5000) -and $null -ne $ready.Result -and ($ready.Result | ConvertFrom-Json).supported) 'Lifecycle child did not initialize'
            $remaining = $child.StandardOutput.ReadToEndAsync()
            $watch = [System.Diagnostics.Stopwatch]::StartNew()
            switch ($ending) {
                'eof' { $child.StandardInput.Close() }
                'parent-exit' {
                    Stop-OneOwned $logicalParent
                    $lifetime = Native 'probeProcessLife' @{fixture_pid=$logicalParent.Id}
                    Write-NewJson $lifetime (Join-Path $OutputDir 'product-parent-retained-handle.json')
                    Assert-Probe ($lifetime.creation_time_available -and $lifetime.wait_state -eq 0 -and $null -eq $lifetime.live_birth) 'Retained terminated process was still considered alive'
                }
                'oversize-line' {
                    $bytes = $Utf8.GetBytes(('A' * 1048577))
                    try { $child.StandardInput.BaseStream.Write($bytes,0,$bytes.Length); $child.StandardInput.BaseStream.Flush() } catch [System.IO.IOException] { }
                }
            }
            Assert-Probe ($child.WaitForExit(3000) -and $remaining.Wait(1000) -and $remaining.Result -ceq '') 'Lifecycle helper did not exit cleanly with no extra stdout'
            return @{ending=$ending;exit_code=$child.ExitCode;exit_ms=$watch.ElapsedMilliseconds;ready=($ready.Result | ConvertFrom-Json);stdout_after_ready=$remaining.Result;parent_identity_only=($ending -eq 'parent-exit')}
        } finally {
            if (-not $child.HasExited) { Stop-OneOwned $child }
            if ($null -ne $logicalParent -and -not $logicalParent.HasExited) { Stop-OneOwned $logicalParent }
            if ($stderr.Wait(1000)) { [System.IO.File]::WriteAllText((Join-Path $OutputDir "product-lifecycle-$ending-stderr.txt"),$stderr.Result,$Utf8) }
        }
    }
}
Product-Case 'TC-WIN-08-product-parent-during-wait' {
    $logicalParent = Start-Owned (Get-Command pwsh).Source @('-NoProfile','-Command','Start-Sleep -Seconds 60')
    $info = [System.Diagnostics.ProcessStartInfo]::new($productInfo.FileName)
    $info.UseShellExecute=$false; $info.CreateNoWindow=$true
    $info.RedirectStandardInput=$true; $info.RedirectStandardOutput=$true; $info.RedirectStandardError=$true
    $info.StandardInputEncoding=$Utf8; $info.StandardOutputEncoding=$Utf8
    $info.ArgumentList.Add('--parent-pid'); $info.ArgumentList.Add([string]$logicalParent.Id)
    $child = [System.Diagnostics.Process]::Start($info); $script:Owned.Add($child)
    $stderr = $child.StandardError.ReadToEndAsync()
    try {
        $child.StandardInput.WriteLine('{"id":1,"action":"permission"}');$child.StandardInput.Flush()
        $ready=$child.StandardOutput.ReadLineAsync()
        Assert-Probe ($ready.Wait(5000) -and $null -ne $ready.Result -and ($ready.Result | ConvertFrom-Json).supported) 'Parent-race helper did not initialize'
        $field=Fixture 'field' @{field='plain'}
        Focus-Target $field.pid
        $null=Native 'probeSeedClipboard'
        $before=Native 'probeClipboardState'
        $held=Native 'probeHoldModifiers' @{fixture_pid=$field.pid}
        Assert-Probe ($held.held -contains 162 -and $held.held -contains 160) 'Race precondition modifiers not held'
        $child.StandardInput.WriteLine('{"id":2,"action":"insertCurrent","insertion_id":"parent-race","text":"MUST NOT PASTE"}');$child.StandardInput.Flush()
        $remaining=$child.StandardOutput.ReadToEndAsync()
        Stop-OneOwned $logicalParent
        Assert-Probe ($child.WaitForExit(3000) -and $remaining.Wait(1000)) 'Parent-race helper remained alive'
        if ($remaining.Result -cne '') {
            $reply=$remaining.Result | ConvertFrom-Json
            Assert-Probe ($reply.id -eq 2 -and $reply.status -eq 'failed' -and $reply.effect -eq 'none') 'Parent-race response admitted a possible write'
        }
        $actual=Fixture 'fieldState'
        $after=Native 'probeClipboardState'
        Assert-Probe ($actual.value -ceq 'BEGIN React END' -and $actual.chords.Count -eq 0 -and $actual.enterCount -eq 0 -and $before.sequence -eq $after.sequence) 'Parent exit allowed a late target/clipboard write'
        return @{held=$held;actual=$actual;before=$before;after=$after;response_after_parent_exit=$remaining.Result;exit_code=$child.ExitCode;scope='parent identity exits while injected modifiers block dispatch; retained process handle and open stdin'}
    } finally {
        $null=Native 'probeReleaseModifiers'
        if (-not $child.HasExited) { Stop-OneOwned $child }
        if (-not $logicalParent.HasExited) { Stop-OneOwned $logicalParent }
        if ($stderr.Wait(1000)) { [System.IO.File]::WriteAllText((Join-Path $OutputDir 'product-parent-race-stderr.txt'),$stderr.Result,$Utf8) }
    }
}
