param(
    [Parameter(Mandatory=$true)][string]$PackageDir,
    [Parameter(Mandatory=$true)][string]$EvidenceDir,
    [string]$NormalPackageDir='',
    [switch]$Worker
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
if($env:GITHUB_ACTIONS -ne 'true'){throw 'Simulation smoke requires a disposable GitHub Windows runner'}
$verify=Join-Path $env:CARGO_TARGET_DIR 'release/verify.exe'
if(-not $Worker){
    $arguments=@('-NoProfile','-File',$PSCommandPath,'-Worker','-PackageDir',$PackageDir,'-EvidenceDir',$EvidenceDir)
    if($NormalPackageDir){$arguments+=@('-NormalPackageDir',$NormalPackageDir)}
    & $verify --medium (Get-Command pwsh).Source @arguments
    if($LASTEXITCODE -ne 0){throw 'Installed simulation smoke failed; complete anonymous case denominator is retained'}
    exit 0
}
$preflight=& $verify --preflight | ConvertFrom-Json
if($LASTEXITCODE -ne 0 -or $preflight.integrity_rid -ne 8192 -or -not $preflight.input_desktop_accessible){throw 'Worker requires an interactive Medium integrity desktop'}
$EvidenceDir=[IO.Path]::GetFullPath($EvidenceDir)
$tempRoot=[IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\')+'\'
if(-not $EvidenceDir.StartsWith($tempRoot,[StringComparison]::OrdinalIgnoreCase) -or (Test-Path -LiteralPath $EvidenceDir)){throw 'EvidenceDir must be a new runner temporary directory'}
[IO.Directory]::CreateDirectory($EvidenceDir)|Out-Null
$private=Join-Path $EvidenceDir 'private'
[IO.Directory]::CreateDirectory($private)|Out-Null
$repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..'))
$nonce=[Guid]::NewGuid().ToString('N')
$installDir=Join-Path $env:RUNNER_TEMP "SVI 模拟 installed $nonce"
$normalInstallDir=Join-Path $env:RUNNER_TEMP "SVI 正常 control $nonce"
$profile=Join-Path $env:APPDATA 'SmartVoiceInput Simulation'
$normalProfile=Join-Path $env:APPDATA 'SmartVoiceInput'
$entry='com.smartvoiceinput.desktop.simulation'
$normalEntry='com.smartvoiceinput.desktop'
$canaryEntry="SVI-Simulation-Canary-$nonce"
$runKey='Software\Microsoft\Windows\CurrentVersion\Run'
$approvedKey='Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
$owned=[Collections.Generic.List[object]]::new()
$helpers=[Collections.Generic.List[object]]::new()
$reports=[Collections.Generic.List[object]]::new()
$script:installed=$false;$script:normalInstalled=$false;$script:canaryRegistered=$false
$script:main=$null;$script:normal=$null;$script:exercise=$null;$script:normalSnapshot=$null
$script:markerHash=$null;$script:normalMarkerHash=$null
$injector=Join-Path $private 'simulation-shortcut.exe'
$probe=Join-Path $env:CARGO_TARGET_DIR 'release/svi-windows-probe.exe'
$restartFile=Join-Path $private 'restart-expectations.json'
$version=(Get-Content -Raw (Join-Path $repo 'desktop/package.json')|ConvertFrom-Json).version
$ids=@('SIM-SETUP','SIM-INSTALL','SIM-HELPER-IDENTITY','SIM-PROCESS-EXIT','SIM-JOURNAL','SIM-STARTUP-REGISTRY','SIM-RESTART-PROCESS-EXIT','SIM-UNINSTALL','SIM-ISOLATION','NORMAL-INSTALL','NORMAL-COEXISTENCE','NORMAL-PROCESS-EXIT','NORMAL-UNINSTALL')
$uiPlan=& node (Join-Path $PSScriptRoot 'simulation-ui.cjs') --plan | ConvertFrom-Json
if($LASTEXITCODE -ne 0){throw 'UI smoke case registry unavailable'}
$data=[ordered]@{
    schema='svi-installed-simulation-smoke-v1';source_sha=$env:GITHUB_SHA;version=$version;status='not_run';integrity_rid=$preflight.integrity_rid
    scope='L2 disposable Windows Server x64, installed NSIS, actual global shortcut, Main/SessionService/journal/native helper and independent external readback'
    artifact_allowlist=@('summary.json','ui-exercise.json','ui-resume.json','ui-normal-configure.json','ui-normal-quit.json')
    private_data_upload_allowed=$false;real_normal_control=[bool]$NormalPackageDir
    cases=@($ids|ForEach-Object{@{id=$_;status='not_run';required=($_ -notlike 'NORMAL-*' -or [bool]$NormalPackageDir)}})
    cleanup=@();phase_failures=@();process_identity=@();helper_observations=@();limitations=@(
        @{id='TC-SIM-04-WeChat';status='not_run';reason='WeChat and its real account are not installed or automated on this runner'},
        @{id='TC-SIM-07-08-fault-matrix';status='not_run';reason='Full repeated-stop, crash/claim/journal dispatch and lifecycle boundary matrix is outside this installed shortcut smoke'},
        @{id='TC-SIM-09-reserved-unverified-recovery';status='not_run';reason='This smoke proves confirmed inserted restart only; reserved/unverified interruption recovery requires separate cases'},
        @{id='TC-SIM-10-11-protection-clipboard';status='not_run';reason='Protected/high-integrity targets and all clipboard formats/concurrent ownership variants retain their existing separate denominator'},
        @{id='TC-SIM-12-14-account-dictionary-matrix';status='not_run';reason='Only fictional login/credential restoration, history and statistics run here; registration/CRUD/conflicts/import matrix is separate'},
        @{id='TC-SIM-15-tray-crash-matrix';status='not_run';reason='Actual menu quit and helper exit run here; full tray, renderer crash and single-instance matrix is separate'},
        @{id='TC-SIM-16-system-login';status='not_run';reason='Registry readback does not prove a real Windows system login'},
        @{id='TC-SIM-18-20-build-security';status='not_run';reason='Runtime mode switches, loopback authorization and module graph/ASAR checks are separate build/security gates'},
        @{id='TC-SIM-21-hardware';status='not_run';reason='Physical keyboard, microphone, IME, multi-screen/DPI, real sleep/login and two-hour stability require actual devices'},
        @{id='TC-SIM-22-removal';status='not_run';reason='Temporary product removal waits for the user to finish their testing'},
        @{id='S5-real-services';status='not_run';reason='No real ASR/LLM, production backend, PostgreSQL or Admin acceptance is inferred'}
    )
}
foreach($phase in $uiPlan.PSObject.Properties){foreach($id in $phase.Value){$data.cases+=@(@{id=$id;status='not_run';required=($phase.Name -notlike 'normal-*' -or [bool]$NormalPackageDir)})}}
function Assert-Smoke([bool]$Ok,[string]$Message){if(-not $Ok){throw $Message}}
function Error-Text($Failure){return $Failure.Exception.Message.Replace($env:USERPROFILE,'[runner-profile]').Replace($repo,'[source]').Replace($env:RUNNER_TEMP,'[runner-temp]')}
function Write-NewJson($Value,[string]$File){
    $bytes=[Text.Encoding]::UTF8.GetBytes(($Value|ConvertTo-Json -Depth 30)+"`n")
    $stream=[IO.File]::Open($File,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try{$stream.Write($bytes,0,$bytes.Length)}finally{$stream.Dispose()}
}
function Case([string]$Id,[scriptblock]$Body){
    $row=$data.cases|Where-Object{$_.id -eq $Id}
    $row.started_at=[DateTime]::UtcNow.ToString('o')
    try{$row.observations=& $Body;$row.status='pass'}
    catch{$row.status='fail';$row.error=Error-Text $_;$row.failure_category=$_.FullyQualifiedErrorId;$row.failure_line=$_.InvocationInfo.ScriptLineNumber}
    $row.ended_at=[DateTime]::UtcNow.ToString('o')
}
function Passed([string]$Id){return ($data.cases|Where-Object{$_.id -eq $Id}).status -eq 'pass'}
function Registry-State([string]$Name){
    $state=[ordered]@{}
    foreach($pair in @(@('run',$runKey),@('approved',$approvedKey))){
        $key=[Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($pair[1],$false)
        try{$exists=$null -ne $key -and $key.GetValueNames().Contains($Name);$state[$pair[0]]=@{exists=$exists;value=$(if($exists){$key.GetValue($Name)}else{$null})}}finally{if($null -ne $key){$key.Dispose()}}
    }
    return $state
}
function Registry-Equal($First,$Second){return ($First|ConvertTo-Json -Depth 8 -Compress) -ceq ($Second|ConvertTo-Json -Depth 8 -Compress)}
function Uninstall-Entries([string]$ProductName){
    $parent=[Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Uninstall',$false)
    $rows=@()
    try{
        if($null -ne $parent){foreach($name in $parent.GetSubKeyNames()){
            $key=$parent.OpenSubKey($name,$false)
            try{
                if($null -ne $key){
                    $display=$key.GetValue('DisplayName')
                    if($display -eq $ProductName -or $display -match ('^'+[Regex]::Escape($ProductName)+' \d+\.\d+\.\d+$')){
                        $command=$key.GetValue('UninstallString');$location=$null
                        if($command -match '^"([^"\r\n]+)"(?:\s|$)'){$location=[IO.Path]::GetDirectoryName($Matches[1])}
                        $rows+=@(@{key=$name;location=$location;version=$key.GetValue('DisplayVersion')})
                    }
                }
            }finally{if($null -ne $key){$key.Dispose()}}
        }}
    }finally{if($null -ne $parent){$parent.Dispose()}}
    return $rows
}
function NSIS([string]$Program,[string]$Arguments){
    $info=[Diagnostics.ProcessStartInfo]::new($Program);$info.UseShellExecute=$false
    # NSIS requires the final /D= or _?= path without quotes, including spaces.
    $info.Arguments=$Arguments;$process=[Diagnostics.Process]::Start($info)
    try{Assert-Smoke ($process.WaitForExit(120000)) 'Owned installer/uninstaller deadline exceeded';Assert-Smoke ($process.ExitCode -eq 0) 'Owned NSIS process returned failure';return @{exit_confirmed=$true;exit_code=$process.ExitCode}}
    finally{if(-not $process.HasExited){$process.Kill($true);Assert-Smoke ($process.WaitForExit(5000)) 'Owned NSIS cleanup was not confirmed'}}
}
function Port {
    $listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0);$listener.Start()
    try{return $listener.LocalEndpoint.Port}finally{$listener.Stop()}
}
function Process-Image([Diagnostics.Process]$Process){
    if(-not ('SimulationSmokeImageQuery' -as [type])){Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class SimulationSmokeImageQuery {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool QueryFullProcessImageNameW(IntPtr process, uint flags, StringBuilder image, ref uint size);
}
'@}
    $size=[uint32]32768;$buffer=[Text.StringBuilder]::new([int]$size)
    if(-not [SimulationSmokeImageQuery]::QueryFullProcessImageNameW($Process.Handle,0,$buffer,[ref]$size)){throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())}
    Assert-Smoke ($size -gt 0 -and $size -lt $buffer.Capacity) 'IDENTITY_IMAGE_QUERY_INVALID_LENGTH'
    return $buffer.ToString(0,[int]$size)
}
function Image-Label([string]$Image){
    if(-not $Image){return $null}
    $full=[IO.Path]::GetFullPath($Image)
    if($env:WINDIR -and $full.StartsWith(([IO.Path]::GetFullPath($env:WINDIR).TrimEnd('\')+'\'),[StringComparison]::OrdinalIgnoreCase)){return 'windows/'+[IO.Path]::GetRelativePath($env:WINDIR,$full).Replace('\','/')}
    return [IO.Path]::GetFileName($full)
}
function Facts([Diagnostics.Process]$Process,[string]$ExpectedImage='',[string]$Symbol='controller'){
    $clock=[Diagnostics.Stopwatch]::StartNew();$verificationClock=[Diagnostics.Stopwatch]::new();$birth=$null
    $observation=@{symbol=$Symbol;status='fail';attempts=1;stage='process-handle';image_source='QueryFullProcessImageNameW';expected_image=(Image-Label $ExpectedImage);ready_wait_ms=0;verification_ms=0;elapsed_ms=0}
    try{
        $null=$Process.Handle
        $observation.stage='process-lifetime';$Process.Refresh()
        Assert-Smoke (-not $Process.HasExited) "Facts[$Symbol] IDENTITY_EXITED before verification"
        $birth=$Process.StartTime.ToUniversalTime().Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)
        $observation.stage='process-image-query';$image=Process-Image $Process
        $observation.observed_image=Image-Label $image
        $observation.ready_wait_ms=$clock.ElapsedMilliseconds;$verificationClock.Start()
        $observation.stage='image-match';$image=[IO.Path]::GetFullPath($image)
        if($ExpectedImage){Assert-Smoke ($image -ieq [IO.Path]::GetFullPath($ExpectedImage)) "Facts[$Symbol] IDENTITY_IMAGE_MISMATCH"}
        $observation.stage='image-hash';$digest=(Get-FileHash -LiteralPath $image -Algorithm SHA256).Hash.ToLowerInvariant()
        $observation.stage='lifetime-readback';$Process.Refresh()
        Assert-Smoke (-not $Process.HasExited -and $Process.StartTime.ToUniversalTime().Ticks.ToString([Globalization.CultureInfo]::InvariantCulture) -ceq $birth) "Facts[$Symbol] IDENTITY_EXITED_OR_CHANGED during verification"
        $observation.stage='verified';$observation.status='verified'
        return @{pid=$Process.Id;birth_ticks=$birth;image=$image;sha256=$digest}
    }catch{
        $observation.failure_category=$_.FullyQualifiedErrorId
        $cause=$_.Exception.GetBaseException()
        if($cause -is [ComponentModel.Win32Exception]){$observation.native_error_code=$cause.NativeErrorCode}
        throw
    }
    finally{
        $observation.elapsed_ms=$clock.ElapsedMilliseconds
        if(-not $verificationClock.IsRunning){$observation.ready_wait_ms=$observation.elapsed_ms}
        $observation.verification_ms=$verificationClock.ElapsedMilliseconds
        $data.process_identity+=@($observation)
    }
}
function Start-Owned([string]$Program,[string[]]$Arguments,[string]$Role,[string]$Name,[string]$Install=''){
    $info=[Diagnostics.ProcessStartInfo]::new($Program);$info.UseShellExecute=$false
    foreach($argument in $Arguments){$info.ArgumentList.Add($argument)}
    $process=[Diagnostics.Process]::Start($info)
    $row=@{process=$process;role=$Role;name=$Name;facts=$null;identity='pending';active=$true;port=$null};$owned.Add($row)
    $facts=Facts $process $Program "start-owned/$Name"
    $facts.role=$Role;$facts.name=$Name
    if($Install){$facts.install_dir=$Install}
    $row.facts=$facts;$row.identity='verified'
    return $row
}
function Start-App([bool]$IsNormal=$false){
    $directory=if($IsNormal){$normalInstallDir}else{$installDir};$name=if($IsNormal){'SmartVoiceInput.exe'}else{'SmartVoiceInput Simulation.exe'}
    $port=Port
    $row=Start-Owned (Join-Path $directory $name) @("--remote-debugging-port=$port",'--remote-debugging-address=127.0.0.1') $(if($IsNormal){'normal-main'}else{'main'}) $(if($IsNormal){'normal-main'}else{'main'}) $directory
    $row.port=$port;return $row
}
function Manifest($Main,[string]$Phase,$Extra=@{}){
    $facts=@($owned|Where-Object{$_.identity -eq 'verified' -and $_.active -and -not $_.process.HasExited}|ForEach-Object{$_.facts})
    $document=@{schema='svi-simulation-smoke-owner-v1';nonce=$nonce;owner=(Facts (Get-Process -Id $PID));targets=$facts;version=$version;evidence_dir=$EvidenceDir;probe_exe=$probe;injector_exe=$injector;restart_file=$restartFile}
    foreach($key in $Extra.Keys){$document[$key]=$Extra[$key]}
    $file=Join-Path $private "manifest-$Phase.json";Write-NewJson $document $file;return $file
}
function UI($Main,[string]$Phase,$Extra=@{}){
    $file=Manifest $Main $Phase $Extra
    $output=Join-Path $EvidenceDir "ui-$Phase.json"
    $info=[Diagnostics.ProcessStartInfo]::new((Get-Command node).Source);$info.UseShellExecute=$false;$info.CreateNoWindow=$true
    foreach($argument in @((Join-Path $PSScriptRoot 'simulation-ui.cjs'),$file,[string]$Main.port,$Phase,$output)){$info.ArgumentList.Add($argument)}
    $info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
    $process=[Diagnostics.Process]::Start($info);$stdout=$process.StandardOutput.ReadToEndAsync();$stderr=$process.StandardError.ReadToEndAsync()
    try{
        $end=[DateTime]::UtcNow.AddSeconds(240)
        while(-not $process.WaitForExit(60)){
            Record-Helpers $Main
            Assert-Smoke ([DateTime]::UtcNow -lt $end) 'Installed UI phase deadline exceeded'
        }
        $code=$process.ExitCode
    }finally{if(-not $process.HasExited){$process.Kill($true);Assert-Smoke ($process.WaitForExit(3000)) 'Owned Node UI driver cleanup was not confirmed'}}
    Assert-Smoke (Test-Path -LiteralPath $output) 'Actual installed UI smoke did not produce a complete report'
    $report=Get-Content -Raw -LiteralPath $output|ConvertFrom-Json
    $reports.Add($report)
    foreach($actual in $report.cases){
        $planned=$data.cases|Where-Object{$_.id -eq $actual.id}
        Assert-Smoke ($null -ne $planned) 'UI returned an unplanned case'
        foreach($property in $actual.PSObject.Properties){$planned[$property.Name]=$property.Value}
    }
    Assert-Smoke ($code -eq 0 -and $report.status -eq 'pass') 'One or more actual installed UI cases failed; full phase denominator retained'
    return $report
}
function Record-Helpers($Main){
    if($Main.process.HasExited){return}
    $children=@(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($Main.process.Id) AND Name = 'smartvoiceinput-input.exe'")
    foreach($child in $children){
        $previous=@($helpers|Where-Object{$_.process.Id -eq $child.ProcessId -and $_.main_pid -eq $Main.process.Id -and -not $_.process.HasExited})
        if($previous.Count){continue}
        $process=$null;$row=$null
        $observation=@{main_kind=$Main.role;candidate_pid=$child.ProcessId;stage='snapshot-to-handle';status='fail';exit_confirmed=$false;verified=$false}
        try{
            try{$process=Get-Process -Id $child.ProcessId -ErrorAction Stop}catch{
                if($_.FullyQualifiedErrorId -notlike 'NoProcessFoundForGivenId*'){throw}
                $observation.status='snapshot-disappeared';$observation.failure_category=$_.FullyQualifiedErrorId
                continue
            }
            $null=$process.SafeHandle
            if($process.WaitForExit(0)){$observation.status='exited-before-verification';$observation.exit_confirmed=$true;continue}
            $observation.stage='snapshot-birth'
            $snapshotBirth=$child.CreationDate.ToUniversalTime().Ticks;$actualBirth=$process.StartTime.ToUniversalTime().Ticks
            # CIM CreationDate exposes microseconds; compare its exact represented precision.
            Assert-Smoke (($actualBirth-$actualBirth%10) -eq ($snapshotBirth-$snapshotBirth%10)) 'HELPER_SNAPSHOT_BIRTH_MISMATCH'
            $row=@{process=$process;facts=$null;identity='pending';main_pid=$Main.process.Id};$helpers.Add($row)
            $observation.stage='facts'
            $facts=Facts $process (Join-Path $Main.facts.install_dir 'resources/voice/smartvoiceinput-input.exe') 'production-helper'
            Assert-Smoke ($process.StartTime -ge $Main.process.StartTime) 'Main helper lifetime differs'
            Assert-Smoke ($child.CommandLine -match "--parent-pid\s+$($Main.process.Id)(?:\s|$)") 'Main helper omitted its actual parent identity'
            $row.facts=$facts;$row.identity='verified'
            $observation.status='verified';$observation.verified=$true
        }catch{
            $failure=$_;$exited=$false
            if($null -ne $process){try{$exited=$process.WaitForExit(0)}catch{}}
            $knownMismatch=$failure.Exception.Message -match 'IDENTITY_(?:IMAGE|HASH)_MISMATCH|IDENTITY_LIFETIME_CHANGED|HELPER_SNAPSHOT_BIRTH_MISMATCH|Main helper lifetime differs|Main helper omitted'
            if($exited -and -not $knownMismatch){
                if($null -ne $row){$row.identity='exited-before-verification'}
                $observation.status='exited-before-verification';$observation.exit_confirmed=$true;$observation.failure_category=$failure.FullyQualifiedErrorId
                continue
            }
            $observation.failure_category=$failure.FullyQualifiedErrorId
            throw
        }finally{$data.helper_observations+=@($observation)}
    }
}
function Observe-Helpers($Main){
    $end=[DateTime]::UtcNow.AddSeconds(15)
    do{
        Record-Helpers $Main
        $children=@($helpers|Where-Object{$_.main_pid -eq $Main.process.Id -and $_.identity -eq 'verified'})
        if($children.Count -ge 1){break}
        Assert-Smoke (-not $Main.process.HasExited) 'Actual installed Main exited before helper observation'
        Start-Sleep -Milliseconds 80
    }while([DateTime]::UtcNow -lt $end)
    Assert-Smoke ($children.Count -ge 1) 'Actual Main did not own its production input helper'
    return @{helper_count=$children.Count;actual_installed_helper=$true;parent_identity_verified=$true}
}
function Exit-Confirmed($Main,[int]$MinimumHelpers=1){
    Assert-Smoke ($Main.process.WaitForExit(8000)) 'Native application menu did not quit the actual installed Main'
    Assert-Smoke ($Main.process.ExitCode -eq 0) 'Actual installed Main did not quit cleanly'
    $Main.active=$false
    $children=@($helpers|Where-Object{$_.main_pid -eq $Main.process.Id -and $_.identity -eq 'verified'})
    Assert-Smoke ($children.Count -ge $MinimumHelpers) 'Not all actually used production helper lifetimes were independently observed'
    foreach($helper in $children){Assert-Smoke ($helper.process.WaitForExit(2500)) 'Owned production helper remained after actual application quit'}
    return @{native_menu_quit=$true;main_exit_confirmed=$true;helper_exit_count=$children.Count;helpers_exited=$true}
}
function Install-Product([string]$Directory,[string]$Destination,[string]$ExeName){
    Assert-Smoke (-not (Test-Path -LiteralPath $Destination)) 'Refusing to replace an existing installation directory'
    Assert-Smoke ($Destination -notmatch '["\r\n]') 'Unsafe NSIS destination'
    $installers=@(Get-ChildItem -LiteralPath $Directory -Filter '*.exe' -File)
    Assert-Smoke ($installers.Count -eq 1) 'Exactly one installer must be frozen in each package directory'
    if($Destination -eq $installDir){$script:installed=$true}else{$script:normalInstalled=$true}
    $result=NSIS $installers[0].FullName "/S /currentuser /D=$Destination"
    foreach($relative in @($ExeName,'resources/voice/smartvoiceinput-input.exe','resources/app.asar')){
        $actual=Join-Path $Destination $relative;$expected=Join-Path (Join-Path $Directory 'win-unpacked') $relative
        Assert-Smoke ((Test-Path -LiteralPath $actual) -and (Get-FileHash -LiteralPath $actual).Hash -eq (Get-FileHash -LiteralPath $expected).Hash) 'Actual installed executable/helper/ASAR differs from the frozen package'
    }
    $voice=@(Get-ChildItem -LiteralPath (Join-Path $Destination 'resources/voice') -File)
    Assert-Smoke ($voice.Count -eq 1 -and $voice[0].Name -eq 'smartvoiceinput-input.exe') 'Test probe or injector was included in the installed product helper directory'
    $productName=[IO.Path]::GetFileNameWithoutExtension($ExeName)
    $entries=@(Uninstall-Entries $productName)
    Assert-Smoke ($entries.Count -eq 1 -and $null -ne $entries[0].location -and $entries[0].location.TrimEnd('\') -ieq $Destination.TrimEnd('\') -and $entries[0].version -eq $version) 'Actual uninstall registry identity/version/directory differs'
    return @{installer_sha256=(Get-FileHash -LiteralPath $installers[0].FullName).Hash;installer_bytes=$installers[0].Length;exe_sha256=(Get-FileHash -LiteralPath (Join-Path $Destination $ExeName)).Hash;helper_sha256=(Get-FileHash -LiteralPath $voice[0].FullName).Hash;asar_sha256=(Get-FileHash -LiteralPath (Join-Path $Destination 'resources/app.asar')).Hash;nsis=$result;unicode_space_install_path=$true;actual_uninstall_registry_identity=$true}
}
function Stable-Normal {
    $files=[ordered]@{}
    foreach($file in @(Get-ChildItem -LiteralPath $normalProfile -File|Where-Object{$_.Name -in @('settings.json','device.json','simulation-canary.txt') -or $_.Name -like 'auth-*'}|Sort-Object Name)){$files[$file.Name]=(Get-FileHash -LiteralPath $file.FullName).Hash}
    return @{files=$files;startup=(Registry-State $normalEntry)}
}
function Journal-Facts {
    $root=Join-Path $profile 'insertion-marks'
    $files=@(Get-ChildItem -LiteralPath $root -Filter '*.json' -File -Recurse)
    $expected=@('user_id','session_id','result_id','insertion_id','text_hash','version','phase','observable','error_code','started_at_ms','receipt_confirmed')|Sort-Object
    $seen=[Collections.Generic.HashSet[string]]::new()
    foreach($file in $files){
        Assert-Smoke ($file.Length -lt 4096) 'Insertion journal is oversized'
        $mark=Get-Content -Raw -LiteralPath $file.FullName|ConvertFrom-Json
        $keys=@($mark.PSObject.Properties.Name|Sort-Object)
        Assert-Smoke (($keys -join ',') -ceq ($expected -join ',')) 'Insertion journal contains fields beyond the metadata allowlist'
        Assert-Smoke ($mark.phase -eq 'inserted' -and $mark.observable -eq $true -and $mark.receipt_confirmed -eq $true -and $null -eq $mark.error_code) 'Journal did not confirm actual observable insertion and receipt'
        Assert-Smoke ($seen.Add($mark.session_id)) 'A result produced more than one local execution journal'
    }
    return @{file_count=$files.Count;unique_session_count=$seen.Count;metadata_fields_only=$true;all_inserted_observable_and_receipt_confirmed=$true;state_or_tokens_exported=$false}
}
function Journal-Snapshot {
    $root=Join-Path $profile 'insertion-marks';$files=[ordered]@{}
    $exists=Test-Path -LiteralPath $root
    if($exists){foreach($file in @(Get-ChildItem -LiteralPath $root -File -Recurse|Sort-Object FullName)){$files[[IO.Path]::GetRelativePath($root,$file.FullName)]=(Get-FileHash -LiteralPath $file.FullName).Hash}}
    return @{exists=$exists;files=$files}
}
function Uninstall-Product([string]$Directory,[string]$ExeName){
    $uninstallers=@(Get-ChildItem -LiteralPath $Directory -Filter 'Uninstall*.exe' -File)
    Assert-Smoke ($uninstallers.Count -eq 1) 'Exactly one owned NSIS uninstaller is required'
    $result=NSIS $uninstallers[0].FullName "/S _?=$Directory"
    Assert-Smoke (-not (Test-Path -LiteralPath (Join-Path $Directory $ExeName)) -and -not (Test-Path -LiteralPath (Join-Path $Directory 'resources/voice/smartvoiceinput-input.exe'))) 'Uninstall left the owned executable or helper'
    Assert-Smoke (@(Uninstall-Entries ([IO.Path]::GetFileNameWithoutExtension($ExeName))).Count -eq 0) 'Uninstall left its own product registry entry'
    return $result
}
try{
    Case 'SIM-SETUP' {
        Assert-Smoke ((Test-Path -LiteralPath $verify) -and (Test-Path -LiteralPath $probe)) 'Prebuilt independent Windows verify/probe binaries are required'
        Assert-Smoke (-not (Test-Path -LiteralPath $profile) -and -not (Test-Path -LiteralPath $normalProfile)) 'Fresh isolated product profiles are required; existing profiles are never adopted'
        Assert-Smoke (@(Uninstall-Entries 'SmartVoiceInput').Count -eq 0 -and @(Uninstall-Entries 'SmartVoiceInput Simulation').Count -eq 0) 'Existing installed products are never adopted'
        foreach($name in @($entry,$normalEntry,$canaryEntry)){$state=Registry-State $name;Assert-Smoke (-not $state.run.exists -and -not $state.approved.exists) 'Refusing to replace an existing product or canary startup registration'}
        $compiler=Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        Assert-Smoke (Test-Path -LiteralPath $compiler) 'CI .NET Framework C# compiler unavailable'
        $framework=Split-Path $compiler -Parent
        $uiaReferences=@('UIAutomationClient.dll','UIAutomationTypes.dll','WindowsBase.dll')|ForEach-Object{Join-Path $framework "WPF/$_"}
        foreach($reference in $uiaReferences){Assert-Smoke (Test-Path -LiteralPath $reference) 'CI Framework UI Automation assemblies unavailable'}
        $referenceArguments=@($uiaReferences|ForEach-Object{"/reference:$_"})
        & $compiler /nologo /target:exe /platform:x64 /reference:System.Web.Extensions.dll @referenceArguments "/out:$injector" (Join-Path $PSScriptRoot '../fixtures/simulation-shortcut.cs')|Out-Null
        Assert-Smoke ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $injector)) 'Independent fixed shortcut injector did not compile'
        [IO.Directory]::CreateDirectory($profile)|Out-Null;[IO.Directory]::CreateDirectory($normalProfile)|Out-Null
        [IO.File]::WriteAllText((Join-Path $profile 'simulation-canary.txt'),'Fictional simulation profile preservation canary.')
        [IO.File]::WriteAllText((Join-Path $normalProfile 'simulation-canary.txt'),'Fictional normal profile preservation canary.')
        $script:markerHash=(Get-FileHash -LiteralPath (Join-Path $profile 'simulation-canary.txt')).Hash
        $script:normalMarkerHash=(Get-FileHash -LiteralPath (Join-Path $normalProfile 'simulation-canary.txt')).Hash
        foreach($subkey in @($runKey,$approvedKey)){$key=[Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($subkey);try{if($subkey -eq $runKey){$key.SetValue($canaryEntry,'Fictional unrelated startup canary',[Microsoft.Win32.RegistryValueKind]::String)}else{$key.SetValue($canaryEntry,[byte[]]@(2,0,0,0,0,0,0,0,0,0,0,0),[Microsoft.Win32.RegistryValueKind]::Binary)}}finally{$key.Dispose()}}
        $script:canaryRegistered=$true;$script:canaryBefore=Registry-State $canaryEntry
        foreach($pair in @(@('note-a','Alpha React Omega'),@('note-b','Second Docker Target'),@('note-empty',''))){
            $file=Join-Path $private ($pair[0]+'.txt');[IO.File]::WriteAllText($file,$pair[1],[Text.UTF8Encoding]::new($true))
            $null=Start-Owned (Join-Path $env:WINDIR 'System32/notepad.exe') @($file) 'notepad' $pair[0]
        }
        $chromeExe=@((Join-Path $env:ProgramFiles 'Google/Chrome/Application/chrome.exe'),(Join-Path ${env:ProgramFiles(x86)} 'Google/Chrome/Application/chrome.exe'))|Where-Object{Test-Path -LiteralPath $_}|Select-Object -First 1
        Assert-Smoke ([bool]$chromeExe) 'Actual Chrome is required for the installed target smoke'
        $script:chromePort=Port;$script:chromeFixture=Join-Path $private 'chrome.html'
        [IO.File]::WriteAllText($chromeFixture,'<!doctype html><meta charset="utf-8"><title>SVI owned simulation fixture</title><style>body{padding:40px}input,textarea{display:block;width:650px;padding:12px;margin:20px;font:20px sans-serif}textarea{height:160px}</style><input id="input" aria-label="Fictional input"><textarea id="textarea" aria-label="Fictional textarea"></textarea><script>window.sviEvents={pastes:0,enters:0};for(const field of document.querySelectorAll("input,textarea")){field.addEventListener("keydown",event=>{if(event.ctrlKey&&event.key.toLowerCase()==="v")window.sviEvents.pastes++;if(event.key==="Enter")window.sviEvents.enters++;});}</script>')
        $uri=[Uri]::new($chromeFixture).AbsoluteUri
        $null=Start-Owned $chromeExe @("--user-data-dir=$(Join-Path $private 'chrome-profile')","--remote-debugging-port=$chromePort",'--remote-debugging-address=127.0.0.1','--no-first-run','--disable-sync','--force-renderer-accessibility',"--app=$uri") 'chrome' 'chrome'
        return @{independent_injector_sha256=(Get-FileHash -LiteralPath $injector).Hash;medium_worker_verified=$true;owned_notepads=3;actual_chrome=$true;profiles_created_by_this_run=$true}
    }
    if(Passed 'SIM-SETUP'){
        if($NormalPackageDir){
            Case 'NORMAL-INSTALL' {
                $result=Install-Product $NormalPackageDir $normalInstallDir 'SmartVoiceInput.exe';$script:normalInstalled=$true
                $script:normal=Start-App $true
                $null=UI $normal 'normal-configure'
                $null=Observe-Helpers $normal
                $script:normalSnapshot=Stable-Normal
                return $result
            }
        }else{foreach($row in $data.cases|Where-Object{$_.id -like 'NORMAL-*'}){$row.reason='No real normal installer control supplied; profile/registry canaries alone do not prove coexistence'}}
        Case 'SIM-INSTALL' {
            if($NormalPackageDir){Assert-Smoke (Passed 'NORMAL-INSTALL') 'Normal control must own a disjoint shortcut before simulation starts'}
            $result=Install-Product $PackageDir $installDir 'SmartVoiceInput Simulation.exe';$script:installed=$true
            $product=(Get-Item -LiteralPath (Join-Path $installDir 'SmartVoiceInput Simulation.exe')).VersionInfo.ProductName
            Assert-Smoke ($product -eq 'SmartVoiceInput Simulation') 'Installed executable product identity differs'
            $script:main=Start-App
            return $result
        }
        if(Passed 'SIM-INSTALL'){
            Case 'SIM-HELPER-IDENTITY' {Observe-Helpers $main}
            try{$script:exercise=UI $main 'exercise' @{chrome_port=$chromePort;chrome_fixture=$chromeFixture}}catch{$data.phase_failures+=@{phase='exercise';error=(Error-Text $_)}}
            Case 'SIM-PROCESS-EXIT' {Exit-Confirmed $main 2}
            Case 'SIM-JOURNAL' {
                Assert-Smoke ($null -ne $exercise) 'Full actual UI phase must pass before journal confirmation'
                $facts=Journal-Facts;Assert-Smoke ($facts.file_count -eq $exercise.expected_successes -and $facts.file_count -eq 11) 'Journal file denominator differs from all 11 successful OS insertions'
                return $facts
            }
            Case 'SIM-STARTUP-REGISTRY' {
                $state=Registry-State $entry
                $expected='"'+(Join-Path $installDir 'SmartVoiceInput Simulation.exe')+'" --background'
                Assert-Smoke ($state.run.exists -and $state.run.value -ceq $expected) 'Simulation startup registry is not its own exact installed command line'
                Assert-Smoke (Registry-Equal $canaryBefore (Registry-State $canaryEntry)) 'Simulation startup changed the unrelated canary'
                return @{simulation_app_id_key=$true;exact_installed_command_and_background=$true;unrelated_canary_unchanged=$true}
            }
            if((Passed 'SIM-PROCESS-EXIT') -and $null -ne $exercise){
                $restart=Get-Content -Raw -LiteralPath $restartFile|ConvertFrom-Json
                $script:main=Start-App
                $null=Observe-Helpers $main
                try{$null=UI $main 'resume' @{restart_notes=$restart.notes;expected_successes=$restart.expected_successes}}catch{$data.phase_failures+=@{phase='resume';error=(Error-Text $_)}}
                Case 'SIM-RESTART-PROCESS-EXIT' {Exit-Confirmed $main}
            }
            if($NormalPackageDir){Case 'NORMAL-COEXISTENCE' {
                Assert-Smoke ($null -ne $normal -and -not $normal.process.HasExited -and $normal.process.Id -ne $main.process.Id) 'Actual normal control did not coexist in its separate process domain'
                Assert-Smoke (Registry-Equal $normalSnapshot (Stable-Normal)) 'Simulation changed normal shortcut/profile/startup data'
                return @{actual_normal_process_alive=$true;separate_install_and_profile=$true;normal_settings_and_startup_unchanged=$true;canaries_alone_used_as_coexistence_proof=$false}
            }}
        }
    }
}catch{$data.infrastructure_error=Error-Text $_}
finally{
    foreach($row in @($owned|Where-Object{$_.role -eq 'main'})){
        try{if(-not $row.process.HasExited){$row.process.Kill($true);$data.cleanup+=@{kind='simulation-main';forced=$true;exit_confirmed=$row.process.WaitForExit(5000)}}}catch{$data.cleanup+=@{kind='simulation-main';forced=$true;exit_confirmed=$false}}
    }
    if($installed){Case 'SIM-UNINSTALL' {
        $journalBefore=Journal-Snapshot
        $result=Uninstall-Product $installDir 'SmartVoiceInput Simulation.exe';$script:installed=$false
        $state=Registry-State $entry
        Assert-Smoke (-not $state.run.exists -and -not $state.approved.exists) 'Simulation uninstall left its own startup values'
        Assert-Smoke ((Get-FileHash -LiteralPath (Join-Path $profile 'simulation-canary.txt')).Hash -eq $markerHash) 'Uninstall changed the owned simulation profile canary'
        $journalAfter=Journal-Snapshot
        Assert-Smoke (($journalBefore|ConvertTo-Json -Depth 8 -Compress) -ceq ($journalAfter|ConvertTo-Json -Depth 8 -Compress)) 'Uninstall changed the actual pre-existing execution journal state'
        return @{nsis=$result;own_run_and_approved_removed=$true;profile_canary_preserved=$true;journal_preexisting=$journalBefore.exists;journal_file_count=$journalBefore.files.Count;journal_state_preserved=$true}
    }}
    if(Passed 'SIM-SETUP'){Case 'SIM-ISOLATION' {
        Assert-Smoke ((Get-FileHash -LiteralPath (Join-Path $normalProfile 'simulation-canary.txt')).Hash -eq $normalMarkerHash) 'Simulation installation/uninstall changed the normal profile canary'
        Assert-Smoke (Registry-Equal $canaryBefore (Registry-State $canaryEntry)) 'Simulation uninstall changed the unrelated startup canary'
        if($NormalPackageDir){
            Assert-Smoke (Registry-Equal $normalSnapshot (Stable-Normal)) 'Simulation uninstall changed normal settings/profile/startup'
            Assert-Smoke ((Get-FileHash -LiteralPath (Join-Path $normalInstallDir 'SmartVoiceInput.exe')).Hash -eq (Get-FileHash -LiteralPath (Join-Path $NormalPackageDir 'win-unpacked/SmartVoiceInput.exe')).Hash) 'Simulation uninstall changed the actual normal installed app'
            Assert-Smoke (@(Uninstall-Entries 'SmartVoiceInput').Count -eq 1) 'Simulation uninstall changed the actual normal uninstall registry entry'
        }
        return @{normal_profile_canary_unchanged=$true;unrelated_run_and_approved_unchanged=$true;actual_normal_control=[bool]$NormalPackageDir}
    }}
    if($null -ne $normal -and -not $normal.process.HasExited){
        try{$null=UI $normal 'normal-quit'}catch{$data.phase_failures+=@{phase='normal-quit';error=(Error-Text $_)}}
        Case 'NORMAL-PROCESS-EXIT' {Exit-Confirmed $normal}
    }
    foreach($row in $owned){
        try{if(-not $row.process.HasExited){$row.process.Kill($true)};$data.cleanup+=@{kind=$row.role;identity=$row.identity;exit_confirmed=$row.process.WaitForExit(5000)}}catch{$data.cleanup+=@{kind=$row.role;identity=$row.identity;exit_confirmed=$false}}
    }
    foreach($helper in $helpers){try{$data.cleanup+=@{kind='production-helper';exit_confirmed=$helper.process.WaitForExit(2500)}}catch{$data.cleanup+=@{kind='production-helper';exit_confirmed=$false}}}
    if($normalInstalled){Case 'NORMAL-UNINSTALL' {
        $result=Uninstall-Product $normalInstallDir 'SmartVoiceInput.exe';$script:normalInstalled=$false
        $state=Registry-State $normalEntry
        Assert-Smoke (-not $state.run.exists -and -not $state.approved.exists) 'Normal control uninstall left its own startup values'
        Assert-Smoke ((Get-FileHash -LiteralPath (Join-Path $profile 'simulation-canary.txt')).Hash -eq $markerHash) 'Normal control uninstall changed the separate simulation profile'
        return @{nsis=$result;normal_startup_removed=$true;simulation_data_preserved=$true}
    }}
    if($canaryRegistered){
        foreach($subkey in @($runKey,$approvedKey)){$key=[Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($subkey,$true);try{if($null -ne $key){$key.DeleteValue($canaryEntry,$false)}}finally{if($null -ne $key){$key.Dispose()}}}
        $remaining=Registry-State $canaryEntry;$data.cleanup+=@{kind='owned-registry-canary';exit_confirmed=(-not $remaining.run.exists -and -not $remaining.approved.exists)}
    }
    foreach($row in $data.cases){if($row.status -eq 'not_run' -and -not $row.Contains('reason')){$row.reason='Setup or prerequisite failed before this planned case'}}
    $required=@($data.cases|Where-Object{$_.required})
    $data.denominator=@{planned=$data.cases.Count;required=$required.Count;pass=@($data.cases|Where-Object{$_.status -eq 'pass'}).Count;fail=@($data.cases|Where-Object{$_.status -eq 'fail'}).Count;not_run=@($data.cases|Where-Object{$_.status -eq 'not_run'}).Count;separate_unexecuted_groups=$data.limitations.Count}
    $failedRequired=@($required|Where-Object{$_.status -ne 'pass'}).Count -gt 0
    $failedCleanup=@($data.cleanup|Where-Object{-not $_.exit_confirmed}).Count -gt 0
    $failedUi=@($reports|Where-Object{$_.status -ne 'pass'}).Count -gt 0 -or $data.phase_failures.Count -gt 0
    $data.status=if($failedRequired -or $failedCleanup -or $failedUi -or $installed -or $normalInstalled -or $data.Contains('infrastructure_error')){'fail'}else{'pass'}
    Write-NewJson $data (Join-Path $EvidenceDir 'summary.json')
    if($data.status -ne 'pass'){throw 'Installed simulation verification or owned cleanup failed; summary preserves every planned case and failure'}
}
