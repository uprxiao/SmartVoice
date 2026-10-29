param([Parameter(Mandatory=$true)][string]$PackageDir,[Parameter(Mandatory=$true)][string]$EvidenceDir,[switch]$Worker)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'probe-files.ps1')
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Installation checks require a disposable CI runner' }
$verify=Join-Path $env:CARGO_TARGET_DIR 'release/verify.exe'
if (-not $Worker) {
    & $verify --medium (Get-Command pwsh).Source -NoProfile -File $PSCommandPath -Worker -PackageDir $PackageDir -EvidenceDir $EvidenceDir
    if ($LASTEXITCODE -ne 0) { throw 'Medium NSIS installation checks failed' }
    exit 0
}
if ((& $verify --preflight | ConvertFrom-Json).integrity_rid -ne 8192) { throw 'Installation must run as a normal Medium user' }
if (Test-Path $EvidenceDir) { throw 'EvidenceDir must be a new run directory' }
[IO.Directory]::CreateDirectory($EvidenceDir)|Out-Null
$installDir=Join-Path $env:RUNNER_TEMP "SVI 安装 test $env:GITHUB_RUN_ID"
if (Test-Path $installDir) { throw 'Refusing to replace an existing installation directory' }
if ($installDir.Contains('"') -or $installDir.Contains("`n") -or $installDir.Contains("`r")) { throw 'Unsafe NSIS directory argument' }
$entry='com.smartvoiceinput.desktop'
$other="SVI-fictional-startup-$env:GITHUB_RUN_ID"
$runKey='Software\Microsoft\Windows\CurrentVersion\Run'
$approvedKey='Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
$data=[ordered]@{source_sha=$env:GITHUB_SHA;status='not_run';scope='actual NSIS on disposable Windows Server, Medium user; installed Main/preload/helper; not Windows10/11 hardware/system-login/S5';rounds=@()}
$installer=@(Get-ChildItem $PackageDir -Filter 'SmartVoiceInput-*.exe' -File)
if ($installer.Count -ne 1) { throw 'Exactly one frozen installer is required' }
function Registry-State([string]$Name) {
    $state=@{}
    foreach ($pair in @(@('run',$runKey),@('approved',$approvedKey))) {
        $key=[Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($pair[1],$false)
        try {
            $exists=$null -ne $key -and $key.GetValueNames().Contains($Name)
            $state[$pair[0]]=@{exists=$exists;value=$(if($exists){$key.GetValue($Name)}else{$null})}
        } finally { if ($null -ne $key) { $key.Dispose() } }
    }
    return $state
}
function Assert-Install([bool]$Ok,[string]$Message) { if(-not $Ok){throw $Message} }
function Run-NSIS([string]$Program,[string]$Arguments) {
    $info=[Diagnostics.ProcessStartInfo]::new($Program)
    $info.UseShellExecute=$false
    # NSIS explicitly requires /D= and _?= as the final, unquoted path (including spaces).
    # ProcessStartInfo invokes the executable directly; no shell interprets these arguments.
    $info.Arguments=$Arguments
    $process=[Diagnostics.Process]::Start($info)
    try {
        Assert-Install ($process.WaitForExit(120000)) 'Owned NSIS process did not exit in 120 seconds'
        Assert-Install ($process.ExitCode -eq 0) "NSIS failed with exit $($process.ExitCode)"
        return @{exit_code=$process.ExitCode;exit_confirmed=$process.HasExited}
    } finally {
        if(-not $process.HasExited){$process.Kill($true);Assert-Install ($process.WaitForExit(5000)) 'NSIS cleanup not confirmed'}
    }
}
$fixtureRegistered=$false
$installed=$false
try {
    $data.installer=@{sha256=(Get-FileHash $installer[0].FullName -Algorithm SHA256).Hash;bytes=$installer[0].Length}
    $original=Registry-State $entry
    $data.original_owned_startup=$original
    $otherOriginal=Registry-State $other
    # The preceding owned package off/on/off test can leave this app's disabled Approved value.
    # It must leave no Run registration; unrelated entries are never adopted or replaced.
    Assert-Install (-not $original.run.exists -and -not $otherOriginal.run.exists -and -not $otherOriginal.approved.exists) 'Refusing to overwrite a pre-existing startup registration'
    foreach($subkey in @($runKey,$approvedKey)) {
        $key=[Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($subkey)
        try {
            if($subkey -eq $runKey){$key.SetValue($other,'SVI fictional unrelated startup item',[Microsoft.Win32.RegistryValueKind]::String)}
            else{$key.SetValue($other,[byte[]]@(2,0,0,0,0,0,0,0,0,0,0,0),[Microsoft.Win32.RegistryValueKind]::Binary)}
        } finally {$key.Dispose()}
    }
    $fixtureRegistered=$true
    $otherBefore=Registry-State $other
    $taskUserDataDir=Join-Path $env:APPDATA 'SmartVoiceInput'
    $marker=Join-Path $taskUserDataDir "svi-fictional-preserved-$env:GITHUB_RUN_ID.txt"
    if(Test-Path $marker){throw 'Refusing to overwrite a profile fixture'}
    [IO.Directory]::CreateDirectory($taskUserDataDir)|Out-Null
    [IO.File]::WriteAllText($marker,'Fictional persisted user file; no account or credentials.')
    $markerHash=(Get-FileHash $marker -Algorithm SHA256).Hash
    foreach($round in @(1,2)) {
        $record=[ordered]@{round=$round;status='not_run'}
        $roundDir=Join-Path $EvidenceDir "round-$round"
        [IO.Directory]::CreateDirectory($roundDir)|Out-Null
        try {
            $record.install=Run-NSIS $installer[0].FullName "/S /currentuser /D=$installDir"
            $installed=$true
            $exe=Join-Path $installDir 'SmartVoiceInput.exe'
            Assert-Install (Test-Path $exe) 'Actual installed executable missing at the requested Unicode/space path'
            $record.installed_exe_sha256=(Get-FileHash $exe -Algorithm SHA256).Hash
            Assert-Install ($record.installed_exe_sha256 -eq (Get-FileHash (Join-Path $PackageDir 'win-unpacked/SmartVoiceInput.exe') -Algorithm SHA256).Hash) 'Installed executable differs from the frozen product'
            $record.installed_helper_sha256=(Get-FileHash (Join-Path $installDir 'resources/voice/smartvoiceinput-input.exe') -Algorithm SHA256).Hash
            Assert-Install ($record.installed_helper_sha256 -eq (Get-FileHash (Join-Path $PackageDir 'win-unpacked/resources/voice/smartvoiceinput-input.exe') -Algorithm SHA256).Hash) 'Installed helper differs from the frozen product'
            foreach($file in @('app.asar','service-ca.crt')) {
                $actualHash=(Get-FileHash (Join-Path $installDir "resources/$file") -Algorithm SHA256).Hash
                Assert-Install ($actualHash -eq (Get-FileHash (Join-Path $PackageDir "win-unpacked/resources/$file") -Algorithm SHA256).Hash) "Installed resource differs: $file"
                $record[$file+'_sha256']=$actualHash
            }
            & (Join-Path $PSScriptRoot 'product-package-smoke.ps1') -Worker -PackageDir $PackageDir -ExecutableDir $installDir -EvidenceFile (Join-Path $roundDir 'product-package.json') -LeaveStartupEnabled
            $record.startup_before_uninstall=Registry-State $entry
            Assert-Install $record.startup_before_uninstall.run.exists 'Actual installed Main did not retain its own Run item'
            # Also seed only this app's approved value, to exercise both uninstall deletions.
            $key=[Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($approvedKey)
            try {$key.SetValue($entry,[byte[]]@(2,0,0,0,0,0,0,0,0,0,0,0),[Microsoft.Win32.RegistryValueKind]::Binary)}finally{$key.Dispose()}
            $uninstaller=@(Get-ChildItem $installDir -Filter 'Uninstall*.exe' -File)
            Assert-Install ($uninstaller.Count -eq 1) 'Actual NSIS uninstaller missing'
            $record.uninstaller_sha256=(Get-FileHash $uninstaller[0].FullName -Algorithm SHA256).Hash
            $record.uninstall=Run-NSIS $uninstaller[0].FullName "/S _?=$installDir"
            $installed=$false
            $record.startup_after_uninstall=Registry-State $entry
            $record.other_after_uninstall=Registry-State $other
            Assert-Install (-not (Test-Path $exe) -and -not (Test-Path (Join-Path $installDir 'resources/voice/smartvoiceinput-input.exe'))) 'Uninstall left executable/helper files'
            Assert-Install (-not $record.startup_after_uninstall.run.exists -and -not $record.startup_after_uninstall.approved.exists) 'Uninstall left application-owned startup values'
            Assert-Install (($otherBefore|ConvertTo-Json -Depth 8 -Compress) -ceq ($record.other_after_uninstall|ConvertTo-Json -Depth 8 -Compress)) 'Uninstall modified the unrelated fixture entry'
            Assert-Install ((Test-Path $marker) -and (Get-FileHash $marker -Algorithm SHA256).Hash -eq $markerHash) 'Uninstall changed the fictional persisted user file'
            $record.user_file_preserved=$true
            $record.status='pass'
        } catch {$record.status='fail';$record.error=$_.Exception.Message;throw}
        finally {$data.rounds+=@($record);Write-NewJson $record (Join-Path $roundDir 'installation.json')}
    }
    $data.status='pass'
} catch {$data.status='fail';$data.error=$_.Exception.Message;throw}
finally {
    if($installed) {
        try {
            $uninstall=@(Get-ChildItem $installDir -Filter 'Uninstall*.exe' -File)
            if($uninstall.Count -eq 1){$data.failure_cleanup=Run-NSIS $uninstall[0].FullName "/S _?=$installDir"}
        } catch {$data.cleanup_error=$_.Exception.Message;$data.status='fail'}
    }
    if($fixtureRegistered) {
        foreach($subkey in @($runKey,$approvedKey)) {
            $key=[Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($subkey,$true)
            try {if($null -ne $key){$key.DeleteValue($other,$false)}}finally{if($null -ne $key){$key.Dispose()}}
        }
        $remaining=Registry-State $other
        $data.fixture_cleanup_confirmed=(-not $remaining.run.exists -and -not $remaining.approved.exists)
        if(-not $data.fixture_cleanup_confirmed){$data.status='fail'}
    }
    Write-NewJson $data (Join-Path $EvidenceDir 'summary.json')
    if($data.status -ne 'pass'){throw 'Installation/uninstallation checks or cleanup failed; evidence retained'}
}
