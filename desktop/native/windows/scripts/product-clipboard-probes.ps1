# Called while the production JSONL helper and independent fixtures are alive.
# Every payload is fictional; only disposable CI clipboard contents may be replaced.
foreach ($kind in @('unicode','html','rtf','dib','files','gdi-private','oversize','bitmap','dib-conflict')) {
    Product-Case "TC-WIN-13-product-$kind" {
        $field = Fixture 'field' @{field='plain'}
        Focus-Target $field.pid
        $seedData = @{kind=$kind}
        if ($kind -eq 'files') {
            $fakeFile = Join-Path $OutputDir 'SmartVoiceInput clip fixture.txt'
            [System.IO.File]::WriteAllText($fakeFile, 'React Docker TypeScript fictional clipboard file', $Utf8)
            $seedData.fixture_file = $fakeFile
        }
        $before = Native 'probeSeedClipboard' $seedData
        $result = Product 'insertCurrent' @{insertion_id="clipboard-$kind";text='中文🙂'}
        $independent = Fixture 'fieldState'
        $after = Native 'probeClipboardState'
        Write-NewJson @{kind=$kind;before=$before;result=$result;independent=$independent;after=$after} (Join-Path $OutputDir "product-clipboard-$kind.json")
        $unsafe = $kind -in @('gdi-private','oversize','bitmap','dib-conflict')
        if ($unsafe) {
            Assert-Probe ($result.status -eq 'failed' -and $result.effect -eq 'none' -and $result.code -eq 'TARGET_UNSUPPORTED') 'Unsafe clipboard was not rejected before writing'
            Assert-Probe ($before.sequence -eq $after.sequence -and $independent.chords.Count -eq 0 -and $independent.value -ceq 'BEGIN React END') 'Unsafe clipboard/target changed on rejection'
        } else {
            Assert-Probe (@($before.expected_payloads | Where-Object { -not $_.readback_payload_matches }).Count -eq 0) 'Seed payload did not independently read back'
            Assert-Probe ($result.status -eq 'inserted' -and $result.effect -eq 'written' -and $result.observable -and $result.clipboardRestored) 'Safe clipboard did not support a verified paste and restore'
            Assert-Probe ($independent.value -ceq 'BEGIN 中文🙂 END' -and $independent.start -eq 10 -and $independent.end -eq 10 -and $independent.chords.Count -eq 1) 'Safe clipboard independent text/caret/event mismatch'
            $null = Product 'release' @{target=$result.target}
        }
        Assert-Probe (($before.formats | ConvertTo-Json -Depth 8 -Compress) -ceq ($after.formats | ConvertTo-Json -Depth 8 -Compress)) 'Clipboard format/byte-length/hash fidelity mismatch'
        return @{kind=$kind;result=$result;formats=$after.formats;skipped=$before.skipped;unsafe_rejected=$unsafe}
    }
}
# Return to a known safe baseline for subsequent independent cases.
$null = Native 'probeSeedClipboard'
