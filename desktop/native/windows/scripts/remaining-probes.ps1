param([Parameter(Mandatory=$true)][string]$OutputDir,[switch]$Worker,[ValidateSet('standard','clipboard','ui','boundaries','sessions')][string]$Suite='standard')
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
if($env:GITHUB_ACTIONS -ne 'true'){throw 'Disposable CI only'}
. (Join-Path $PSScriptRoot 'probe-files.ps1')
$verify=Join-Path $env:CARGO_TARGET_DIR 'release/verify.exe'
$probe=Join-Path $env:CARGO_TARGET_DIR 'release/svi-windows-probe.exe'
$formal=Join-Path $env:CARGO_TARGET_DIR 'x86_64-pc-windows-msvc/release/smartvoiceinput-input.exe'
$desktop=Split-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) -Parent
$electron=Join-Path $desktop 'node_modules/electron/dist/electron.exe'
$fixtureExe=Join-Path $env:SVI_PROBE_BUNDLE 'win-unpacked/SVI Windows Probe.exe'
$utf8=[Text.UTF8Encoding]::new($false)
$script:owned=[Collections.Generic.List[Diagnostics.Process]]::new()
$script:cleanup=[Collections.Generic.List[object]]::new()
$script:results=[ordered]@{}
$script:boundaryCleanupOk=$true
$planned=switch($Suite){
 'clipboard'{@('R14-snapshot-publication-race','R14-partial-write-restore','R14-partial-and-restore-failure')}
 'ui'{@('R22-first-close-persist','R22-restart-close','R22-write-failure','R23-renderer-crash-reopen')}
 'boundaries'{@('R03-exact-hwnd-reuse','R06-process-query-denied','R06-no-foreground','R06-inaccessible-input-desktop','R04-natural-cold-chrome')}
 'sessions'{foreach($stage in @('prepare','claim','journal','helper','late')){foreach($boundary in @('cancel','account','suspend')){"R08-$stage-$boundary"}}}
 default{@('R03-released-target','R06-disabled-after-capture','R09-capacity-10000','R09-bridge-crash','R09-bridge-timeout','R08-bridge-invalidate','R06-bridge-own-window','R10-invalid-fields','R10-actions','R10-max-utf16','R04-notepad-truncated-uia','R01-chrome-textarea-lf','R01-chrome-textarea-crlf','R22-first-close-persist','R22-restart-close','R22-write-failure','R23-window-concurrency','R23-window-rebuild','R32-production-hud-15')}
}
foreach($id in $planned){$script:results[$id]=@{id=$id;status='not_run';reason='not attempted'}}
function Check([bool]$Ok,[string]$Message){if(-not $Ok){throw $Message}}
function Start-Owned([string]$Exe,[string[]]$Arguments,[switch]$Redirect,[hashtable]$ExtraEnvironment=@{}){
 $info=[Diagnostics.ProcessStartInfo]::new($Exe);$info.UseShellExecute=$false;$info.CreateNoWindow=$true
 if($Redirect){$info.RedirectStandardInput=$true;$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true;$info.StandardInputEncoding=$utf8;$info.StandardOutputEncoding=$utf8}
 foreach($arg in $Arguments){$info.ArgumentList.Add($arg)}
 foreach($key in $ExtraEnvironment.Keys){$info.Environment[$key]=[string]$ExtraEnvironment[$key]}
 $p=[Diagnostics.Process]::Start($info);$script:owned.Add($p);return $p
}
function Stop-Owned([Diagnostics.Process]$Process){
 if(-not $Process.HasExited){$Process.Kill($true)}
 $exited=$Process.WaitForExit(3500);$script:cleanup.Add(@{pid=$Process.Id;exit_confirmed=$exited});Check $exited 'Owned process exit not confirmed'
}
if(-not $Worker){
 if(Test-Path $OutputDir){throw 'New evidence directory required'}
 [IO.Directory]::CreateDirectory($OutputDir)|Out-Null
 $parent=& $verify --preflight|ConvertFrom-Json
 Write-NewJson $parent (Join-Path $OutputDir 'parent-preflight.json')
 Check $parent.input_desktop_accessible 'No interactive desktop'
 $env:SVI_REMAINING_ROOT=$OutputDir
 $plan=@{source_sha=$env:GITHUB_SHA;frozen_at=[DateTime]::UtcNow.ToString('o');planned=$planned;scope='only additional parameters; existing56/15/fullCI reused by unchanged production source, not rerun or assumed passed here';static_helper_sha256=(Get-FileHash $formal).Hash;module_sha256=(Get-FileHash (Join-Path $env:SVI_REMAINING_MODULE_DIR 'out/main/remaining-modules.cjs')).Hash;physical_keyboard_verified=$false;audio_model_s5_status='not_run';inputs=@()}
 $plan.suite=$Suite;$plan.electron_image_present=Test-Path $electron
 $plan.fault_controls_absent_from_formal_exe=-not ([Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($formal)).Contains('SVI_CLIPBOARD_FAULT_TESTS'))
 Check $plan.fault_controls_absent_from_formal_exe 'Fault control leaked into formal helper'
 if($Suite -eq 'clipboard'){$plan.test_executable_sha256=(Get-FileHash $env:SVI_CLIPBOARD_UNIT_EXE).Hash}
 if($Suite -eq 'boundaries'){$plan.handle_fixture_sha256=(Get-FileHash $env:SVI_HANDLE_REUSE_EXE).Hash}
 $repo=Split-Path $desktop -Parent
 foreach($file in @('desktop/src/main/core/native-input.ts','desktop/src/main/app/window.ts','desktop/src/main/app/windows-tray.ts','desktop/native/windows/scripts/remaining-probes.ps1','desktop/native/windows/scripts/remaining-dialog-ui.ps1','desktop/native/windows/fixtures/remaining-electron.cjs','desktop/native/windows/fixtures/remaining-modules.ts','desktop/native/windows/fixtures/remaining-sessions.cjs','desktop/src/main/index.ts','desktop/src/main/core/session-service.ts','desktop/src/main/core/insertion-journal.ts','desktop/native/windows/fixtures/handle-reuse.cs','desktop/native/windows/fixtures/dialog-native.cs','desktop/native/windows/fixtures/remaining-win32-boundaries.cs','docs/04-test/test-cases-windows-v002.md')){$plan.inputs+=@{path=$file;sha256=(Get-FileHash (Join-Path $repo $file)).Hash}}
 Write-NewJson $plan (Join-Path $OutputDir 'plan.json')
 try{
  $workerProcess=Start-Owned $verify @('--medium',(Get-Command pwsh).Source,'-NoProfile','-File',$PSCommandPath,'-Worker','-OutputDir',$OutputDir,'-Suite',$Suite) -Redirect
  $outTask=$workerProcess.StandardOutput.ReadToEndAsync();$errTask=$workerProcess.StandardError.ReadToEndAsync()
  Check ($workerProcess.WaitForExit(1200000)) 'Supplemental worker exceeded20min; no success inferred'
  Check ($outTask.Wait(2000) -and $errTask.Wait(2000)) 'Worker pipes did not finish'
  [IO.File]::WriteAllText((Join-Path $OutputDir 'worker.stdout.txt'),$outTask.Result,$utf8)
  [IO.File]::WriteAllText((Join-Path $OutputDir 'worker.stderr.txt'),$errTask.Result,$utf8)
  Check ($workerProcess.ExitCode -eq 0) 'Additional parameters failed or blocked; preserve complete results'
 }finally{foreach($p in $script:owned){try{Stop-Owned $p}catch{Write-Warning $_.Exception.Message}};Write-NewJson @($script:cleanup) (Join-Path $OutputDir 'parent-cleanup.json')}
 exit 0
}
$preflight=& $verify --preflight|ConvertFrom-Json
Check ($preflight.integrity_rid -eq 8192 -and $preflight.input_desktop_accessible) 'Actual Medium desktop required'
Write-NewJson $preflight (Join-Path $OutputDir 'medium-preflight.json')
$env:SVI_WINDOWS_PROBE='1'
$script:native=Start-Owned $probe @() -Redirect
$nativeErr=$script:native.StandardError.ReadToEndAsync();$script:nativeId=0
$script:product=Start-Owned $formal @('--parent-pid',[string]$PID) -Redirect
$productErr=$script:product.StandardError.ReadToEndAsync();$script:productId=0
$log=[IO.StreamWriter]::new([IO.File]::Open((Join-Path $OutputDir 'calls.jsonl'),[IO.FileMode]::CreateNew),$utf8)
$script:fileSequence=0;$script:fixtureId=0;$script:hostId=0
$fixtureDir=Join-Path $OutputDir 'field-fixture';[IO.Directory]::CreateDirectory($fixtureDir)|Out-Null
$fixtureProcess=Start-Owned $fixtureExe @('--probe-dir',$fixtureDir)
function Read-Reply([Diagnostics.Process]$Process,[hashtable]$Request,[object]$ExpectedId=$null){
 $Process.StandardInput.WriteLine(($Request|ConvertTo-Json -Depth 10 -Compress));$Process.StandardInput.Flush()
 $task=$Process.StandardOutput.ReadLineAsync();Check ($task.Wait(6500) -and $null -ne $task.Result) 'Actual process did not respond; do not replay'
 $reply=$task.Result|ConvertFrom-Json;if($null -eq $ExpectedId){$ExpectedId=$Request.id};Check ($reply.id -eq $ExpectedId) 'Wrong response id'
 $log.WriteLine((@{process=$Process.Id;request=$Request;response=$reply}|ConvertTo-Json -Compress -Depth 20));$log.Flush();return $reply
}
function Native([string]$Action,[hashtable]$Data=@{}){$script:nativeId++;return Read-Reply $script:native (@{id=$script:nativeId;action=$Action}+$Data)}
function Product([string]$Action,[hashtable]$Data=@{}){$script:productId++;return Read-Reply $script:product (@{id=$script:productId;action=$Action}+$Data)}
function File-Rpc([string]$Folder,[hashtable]$Request,[int]$BudgetMs=20000){
 $script:fileSequence++;$prefix=[string]$script:fileSequence
 $tmp=Join-Path $Folder "$prefix.request.tmp";Write-NewJson $Request $tmp;[IO.File]::Move($tmp,(Join-Path $Folder "$prefix.request.json"))
 $out=Join-Path $Folder "$prefix.response.json";$end=[DateTime]::UtcNow.AddMilliseconds($BudgetMs)
 while(-not (Test-Path $out)){Check ([DateTime]::UtcNow -lt $end) 'Owned fixture command deadline';Start-Sleep -Milliseconds 30}
 $result=Get-Content -Raw $out|ConvertFrom-Json
 if($result.PSObject.Properties.Name -contains 'error'){throw $result.error};return $result
}
function Field([string]$Action,[hashtable]$Data=@{}){return File-Rpc $fixtureDir (@{action=$Action}+$Data)}
function Host-Rpc([string]$Action,[hashtable]$Data=@{}){return File-Rpc $script:hostFolder (@{action=$Action}+$Data)}
function Wait-File([string]$File){$end=[DateTime]::UtcNow.AddSeconds(30);while(-not(Test-Path $File)){Check ([DateTime]::UtcNow -lt $end) 'Owned fixture readiness failed';Start-Sleep -Milliseconds 50}}
function Focus-Field([string]$Name='plain'){
 $field=Field 'field' @{field=$Name;initial='BEGIN React END';start=6;end=11}
 Check ((Native 'probeFocus' @{fixture_pid=$field.pid}).focused) 'Owned input did not focus'
 $attempts=@();$end=[DateTime]::UtcNow.AddSeconds(4)
 do{
  $baseline=Native 'capture' @{fixture_pid=$field.pid};$attempts+=@{text=$baseline.readback.text;caret=$baseline.readback.caret_utf16;selection=$baseline.readback.selection_utf16_len}
  if($baseline.readback.text -ceq 'BEGIN React END' -and $baseline.readback.caret_utf16 -eq 6 -and $baseline.readback.selection_utf16_len -eq 5){return $field}
  Start-Sleep -Milliseconds 40
 }while([DateTime]::UtcNow -lt $end)
 $script:fileSequence++;Write-NewJson @{attempts=$attempts;dom=(Field 'fieldState')} (Join-Path $OutputDir "field-precondition-$($script:fileSequence).json")
 throw 'BLOCKED: readable selected field precondition not acquired; DOM/UIA trace retained'
}
function Case([string]$Id,[scriptblock]$Body){
 if(-not $script:results.Contains($Id)){return}
 $start=[DateTime]::UtcNow
 try{$detail=& $Body;$script:results[$Id]=@{id=$Id;status='pass';observations=$detail;started_at=$start.ToString('o');ended_at=[DateTime]::UtcNow.ToString('o')}}
 catch{$script:results[$Id]=@{id=$Id;status=$(if($_.Exception.Message.StartsWith('BLOCKED:')){'blocked'}else{'fail'});error=$_.Exception.Message;started_at=$start.ToString('o');ended_at=[DateTime]::UtcNow.ToString('o')}}
 Write-Host "$Id $($script:results[$Id].status)"
}
function Open-Host([string]$Mode,[string]$UserDataDir){
 $script:hostId++;$script:hostFolder=Join-Path $OutputDir "host-$($script:hostId)";[IO.Directory]::CreateDirectory($script:hostFolder)|Out-Null
 [IO.Directory]::CreateDirectory($UserDataDir)|Out-Null
 $script:hostProcess=Start-Owned $electron @((Join-Path $desktop 'native/windows/fixtures/remaining-electron.cjs'),$script:hostFolder,$UserDataDir,$Mode) -Redirect
 $script:hostOut=$script:hostProcess.StandardOutput.ReadToEndAsync();$script:hostErr=$script:hostProcess.StandardError.ReadToEndAsync()
 Wait-File (Join-Path $script:hostFolder 'ready.json')
 $ready=Get-Content -Raw (Join-Path $script:hostFolder 'ready.json')|ConvertFrom-Json
 Check ($ready.pid -eq $script:hostProcess.Id) 'Wrong integration process identity';return $ready
}
function Close-Host(){
 if(-not $script:hostProcess.HasExited){$null=Host-Rpc 'quit'}
 Check ($script:hostProcess.WaitForExit(5000)) 'Electron host exit not confirmed'
 Check ($script:hostOut.Wait(1000) -and $script:hostErr.Wait(1000)) 'Electron host output did not finish'
 [IO.File]::WriteAllText((Join-Path $OutputDir "host-$($script:hostId).stderr.txt"),$script:hostErr.Result,$utf8)
 Check ($script:hostProcess.ExitCode -eq 0) 'Electron host failed'
}
function Dialog([string]$Action='inspect'){
 $script:fileSequence++;$file=Join-Path $OutputDir "dialog-$($script:fileSequence).json"
 $p=Start-Owned (Join-Path $env:WINDIR 'System32/WindowsPowerShell/v1.0/powershell.exe') @('-NoProfile','-NonInteractive','-File',(Join-Path $PSScriptRoot 'remaining-dialog-ui.ps1'),'-TargetPid',[string]$script:hostProcess.Id,'-OwnerPid',[string]$PID,'-ExpectedExeSha256',(Get-FileHash $electron).Hash,'-OutputFile',$file,'-NativeDll',$env:SVI_DIALOG_NATIVE_DLL,'-ExpectedNativeDllSha256',(Get-FileHash $env:SVI_DIALOG_NATIVE_DLL).Hash,'-Action',$Action) -Redirect
 $dialogOut=$p.StandardOutput.ReadToEndAsync();$dialogErr=$p.StandardError.ReadToEndAsync()
 Check ($p.WaitForExit(15000) -and $dialogOut.Wait(1000) -and $dialogErr.Wait(1000)) 'Owned native dialog observer timeout'
 [IO.File]::WriteAllText((Join-Path $OutputDir "dialog-$($script:fileSequence).stdout.txt"),$dialogOut.Result,$utf8)
 [IO.File]::WriteAllText((Join-Path $OutputDir "dialog-$($script:fileSequence).stderr.txt"),$dialogErr.Result,$utf8)
 Check ($p.ExitCode -eq 0 -and (Test-Path $file)) 'Owned native dialog inspection/action failed; raw error retained'
 return Get-Content -Raw $file|ConvertFrom-Json
}
Add-Type @'
using System;using System.Runtime.InteropServices;using System.Text;
public class RemainingEditFacts { public string text;public uint start;public uint end;public long hwnd; }
public static class RemainingWin32 {
 [DllImport("user32.dll", SetLastError=true)] public static extern bool PostMessage(IntPtr hWnd,uint msg,IntPtr wParam,IntPtr lParam);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern IntPtr FindWindowEx(IntPtr parent,IntPtr after,string cls,string title);
 [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint pid);
 [DllImport("user32.dll",CharSet=CharSet.Unicode,EntryPoint="SendMessageTimeoutW")] static extern IntPtr SendRaw(IntPtr hwnd,uint msg,UIntPtr wp,IntPtr lp,uint flags,uint timeout,out UIntPtr result);
 [DllImport("user32.dll",CharSet=CharSet.Unicode,EntryPoint="SendMessageTimeoutW")] static extern IntPtr SendText(IntPtr hwnd,uint msg,UIntPtr wp,StringBuilder lp,uint flags,uint timeout,out UIntPtr result);
 public static RemainingEditFacts ReadClassic(IntPtr root,uint pid) {
  IntPtr edit=FindWindowEx(root,IntPtr.Zero,"Edit",null);uint actualPid;GetWindowThreadProcessId(edit,out actualPid);
  if(edit==IntPtr.Zero||actualPid!=pid)throw new Exception("Not the exact owned classic Edit");
  UIntPtr size;if(SendRaw(edit,14,UIntPtr.Zero,IntPtr.Zero,34,1000,out size)==IntPtr.Zero)throw new Exception("Owned text length unavailable");
  int length=checked((int)size.ToUInt64());if(length<0||length>200000)throw new Exception("Owned read exceeds fixed budget");
  StringBuilder buffer=new StringBuilder(length+1);UIntPtr copied;
  if(SendText(edit,13,new UIntPtr((uint)(length+1)),buffer,34,1000,out copied)==IntPtr.Zero||copied.ToUInt64()!=(ulong)length)throw new Exception("Owned full text unavailable");
  UIntPtr selected;if(SendRaw(edit,176,UIntPtr.Zero,IntPtr.Zero,34,1000,out selected)==IntPtr.Zero)throw new Exception("Owned selection unavailable");
  uint packed=(uint)selected.ToUInt64();if(packed==uint.MaxValue)throw new Exception("Selection is outside packed16-bit fixture range");
  return new RemainingEditFacts{text=buffer.ToString(),start=packed&65535,end=packed>>16,hwnd=edit.ToInt64()};
 }
}
'@
function Close-Main([object]$KnownState=$null){
 $state=if($null -ne $KnownState){$KnownState}else{Host-Rpc 'state'};Check ($null -ne $state.main -and $state.pid -eq $script:hostProcess.Id) 'Exact owned Main unavailable'
 Check ([RemainingWin32]::PostMessage([IntPtr]::new([long]$state.main.hwnd),0x10,[IntPtr]::Zero,[IntPtr]::Zero)) 'WM_CLOSE was not posted'
}
function Wait-Host([scriptblock]$Predicate,[int]$Budget=8000){$end=[DateTime]::UtcNow.AddMilliseconds($Budget);do{$state=Host-Rpc 'state';if(& $Predicate $state){return $state};Start-Sleep -Milliseconds 40}while([DateTime]::UtcNow -lt $end);throw 'Production module state did not reach the independent expectation'}
try{
 Wait-File (Join-Path $fixtureDir 'ready.json')
 $null=Native 'ping';$permission=Product 'permission';Check ($permission.supported -and $permission.trusted) 'Formal helper unavailable'
 if($Suite -eq 'clipboard'){
  foreach($kind in @('snapshot-publication-race','partial-write-restore','partial-and-restore-failure')){
   Case "R14-$kind" {
    $field=Focus-Field;$null=Native 'probeSeedClipboard';$before=Native 'probeClipboardState'
    $test=switch($kind){'snapshot-publication-race'{'snapshot_to_publish_foreign_copy_is_zero_write'}'partial-write-restore'{'partial_write_failure_restores_original_without_paste'}default{'partial_write_and_restore_failure_is_visible_without_repeat'}}
    $unit=Start-Owned $env:SVI_CLIPBOARD_UNIT_EXE @("clipboard::fault_checks::$test",'--exact','--ignored','--nocapture','--test-threads=1') -Redirect -ExtraEnvironment @{SVI_CLIPBOARD_FAULT_TESTS='1';SVI_CLIPBOARD_FAULT_TARGET_PID=[string]$field.pid}
    $unitOut=$unit.StandardOutput.ReadToEndAsync();$unitErr=$unit.StandardError.ReadToEndAsync()
    Check ($unit.WaitForExit(30000) -and $unitOut.Wait(1000) -and $unitErr.Wait(1000)) 'Clipboard control did not exit within30sec'
    [IO.File]::WriteAllText((Join-Path $OutputDir "clipboard-$kind.stdout.txt"),$unitOut.Result,$utf8);[IO.File]::WriteAllText((Join-Path $OutputDir "clipboard-$kind.stderr.txt"),$unitErr.Result,$utf8)
    Check ($unit.ExitCode -eq 0 -and $unitOut.Result.Contains('running 1 test') -and $unitOut.Result.Contains('1 passed; 0 failed')) 'Controlled production clipboard experiment failed'
    $match=[regex]::Match($unitOut.Result,'CLIPBOARD_CONTROL_RESULT ([^\r\n]+)');Check $match.Success 'Actual control result missing';$control=$match.Groups[1].Value|ConvertFrom-Json
    $actual=Field 'fieldState';$after=Native 'probeClipboardState';Check ($actual.value -ceq 'BEGIN React END' -and $actual.chords.Count -eq 0 -and $actual.enterCount -eq 0) 'Clipboard error allowed a paste/Enter'
    if($kind -eq 'snapshot-publication-race'){Check ($after.text -ceq 'SVI intervening clipboard' -and $before.sequence -ne $after.sequence) 'External version not preserved'}
    elseif($kind -eq 'partial-write-restore'){Check ($after.text -ceq 'SVI original clipboard') 'Actual rollback did not restore original'}
    else{Check ([string]::IsNullOrEmpty($after.text) -and $after.formats.Count -eq 0) 'Observed restore failure was hidden by an automatic second write'}
    return @{control=$control;actual=$actual;before=$before;after=$after;unit_exit=$unit.ExitCode;scope='actual production dispatch/Win32 state; test-executable-only scheduling or I/O fault; not natural OS failure/S5'}
   }
  }
 }elseif($Suite -eq 'sessions'){
  $null=Open-Host 'normal' (Join-Path $OutputDir 'session-profile')
  foreach($stage in @('prepare','claim','journal','helper','late')){foreach($boundary in @('cancel','account','suspend')){
   Case "R08-$stage-$boundary" {
    $field=Focus-Field;$null=Native 'probeSeedClipboard';$before=Native 'probeClipboardState'
    $prepared=Host-Rpc 'sessionPrepare' @{stage=$stage;boundary=$boundary}
    $null=Host-Rpc 'sessionBegin';$end=[DateTime]::UtcNow.AddSeconds(6)
    do{$state=Host-Rpc 'sessionState';if($state.signal -eq $stage -or ($stage -in @('helper','late') -and $state.signal -eq 'dispatched')){break};Start-Sleep -Milliseconds 30}while([DateTime]::UtcNow -lt $end)
    Check ([bool]$state.signal) 'BLOCKED: exact session stage not acquired'
    if($stage -in @('helper','late')){
     do{$actual=Field 'fieldState';if($actual.value -ceq 'BEGIN 中文🙂 END' -and $actual.chords.Count -eq 1){break};Start-Sleep -Milliseconds 30}while([DateTime]::UtcNow -lt $end)
     Check ($actual.value -ceq 'BEGIN 中文🙂 END' -and $actual.chords.Count -eq 1) 'BLOCKED: actual target consumption before cancellation not acquired'
     if($stage -eq 'late'){
      do{$state=Host-Rpc 'sessionCollectResponse';if($state.actual_write_response_held){break};Start-Sleep -Milliseconds 30}while([DateTime]::UtcNow -lt $end)
      Check $state.actual_write_response_held 'BLOCKED: actual helper write response not yet available for delayed delivery'
     }
    }
    if(-not $state.boundary_applied){$boundaryState=Host-Rpc 'sessionBoundary'}else{$boundaryState=$state}
    $end=[DateTime]::UtcNow.AddSeconds(7)
    do{$settled=Host-Rpc 'sessionState';if($settled.pending.status -ne 'pending'){break};Start-Sleep -Milliseconds 30}while([DateTime]::UtcNow -lt $end)
    Check ($settled.pending.status -ne 'pending') 'Old production session did not settle'
    $after=Field 'fieldState';$clip=Native 'probeClipboardState';$expectedCount=if($stage -in @('helper','late')){1}else{0};$expectedText=if($expectedCount -eq 1){'BEGIN 中文🙂 END'}else{'BEGIN React END'}
    $new=Host-Rpc 'sessionNewOperation';$final=Field 'fieldState';$done=Host-Rpc 'sessionDispose'
    $script:fileSequence++;Write-NewJson @{prepared=$prepared;boundary=$boundaryState;settled=$settled;actual=$after;new_operation=$new;final=$final;cleanup=$done} (Join-Path $OutputDir "session-$stage-$boundary-$($script:fileSequence).json")
    Check ($settled.writes.Count -eq $expectedCount -and $after.value -ceq $expectedText -and $after.chords.Count -eq $expectedCount -and $after.enterCount -eq 0 -and $final.value -ceq $expectedText -and $final.chords.Count -eq $expectedCount -and $clip.text -ceq 'SVI original clipboard' -and $done.exit_confirmed) 'Old session dispatched/replayed after its software boundary or leaked into the next operation'
    return @{boundary=$boundaryState;settled=$settled;actual=$after;new_operation=$new;cleanup=$done;scope='actual SessionService/journal/bridge/helper and loopback WS; metadata/zeroPCM fixture; exact Main interrupt callback injected as software event, not physical sleep/full Main/S5'}
   }
  }}
  Close-Host
 }elseif($Suite -eq 'boundaries'){
  Add-Type -Path (Join-Path $desktop 'native/windows/fixtures/remaining-win32-boundaries.cs')
  $reuseFolder=Join-Path $OutputDir 'owned-native-handle';[IO.Directory]::CreateDirectory($reuseFolder)|Out-Null
  $reuseProcess=Start-Owned $env:SVI_HANDLE_REUSE_EXE @($reuseFolder)
  Wait-File (Join-Path $reuseFolder 'ready.json');$reuseReady=Get-Content -Raw (Join-Path $reuseFolder 'ready.json')|ConvertFrom-Json
  Check ($reuseReady.pid -eq $reuseProcess.Id) 'Owned classic fixture identity differs'
  Case 'R03-exact-hwnd-reuse' {
   Check ((Native 'probeFocus' @{fixture_pid=$reuseProcess.Id}).focused) 'Owned native root unavailable';$null=File-Rpc $reuseFolder @{action='focus'}
   $original=Native 'capture' @{fixture_pid=$reuseProcess.Id};Check ($original.readback.text -ceq 'BEGIN React END' -and $original.readback.caret_utf16 -eq 6 -and $original.readback.selection_utf16_len -eq 5) 'BLOCKED: original classic field not acquired'
   $target=Product 'capture';Check $target.observable 'BLOCKED: original target not observable'
   $prepared=File-Rpc $reuseFolder @{action='reuse'} 85000
   Write-NewJson $prepared (Join-Path $OutputDir 'handle-reuse-preparation.json')
   Check ($prepared.actual_same_hwnd_acquired -and $prepared.state.edit_hwnd -eq $reuseReady.edit_hwnd) 'BLOCKED: exact HWND reuse not observed within100000/60sec; full denominator retained'
   $fresh=Native 'capture' @{fixture_pid=$reuseProcess.Id};Check ($fresh.readback.text -ceq 'BEGIN React END' -and $fresh.readback.caret_utf16 -eq 6 -and $fresh.readback.selection_utf16_len -eq 5) 'BLOCKED: replacement field is not independently readable'
   $null=Native 'probeSeedClipboard';$before=Native 'probeClipboardState';$validation=Product 'validate' @{target=$target.target};$result=Product 'insert' @{target=$target.target;restoreFocus=$false;insertion_id='reused-native-hwnd';text='MUST NOT WRITE'}
   $actual=File-Rpc $reuseFolder @{action='state'};$after=Native 'probeClipboardState'
   Check (-not $validation.valid -and $result.status -eq 'failed' -and $result.effect -eq 'none' -and $actual.text -ceq 'BEGIN React END' -and $actual.start -eq 6 -and $actual.end -eq 11 -and $actual.paste_count -eq 0 -and $actual.enter_count -eq 0 -and $before.sequence -eq $after.sequence) 'Old target rebound to a different control after actual handle reuse'
   return @{original=$original;target=$target;preparation=$prepared;fresh=$fresh;validation=$validation;result=$result;actual=$actual;clipboard_unchanged=$true}
  }
  Case 'R06-process-query-denied' {
   $null=File-Rpc $reuseFolder @{action='reset'}
   Check ((Native 'probeFocus' @{fixture_pid=$reuseProcess.Id}).focused) 'Owned native root unavailable';$null=File-Rpc $reuseFolder @{action='focus'}
   $baseline=Native 'capture' @{fixture_pid=$reuseProcess.Id};Check ($baseline.readback.text -ceq 'BEGIN React END' -and $baseline.readback.caret_utf16 -eq 6 -and $baseline.readback.selection_utf16_len -eq 5) 'BLOCKED: independent lifecycle-query fixture baseline unavailable'
   $null=Native 'probeSeedClipboard';$before=Native 'probeClipboardState'
   try{
    [RemainingBoundaryControl]::DenyOwnedProcessQuery([uint32]$reuseProcess.Id)
    $admission=Native 'foreground';Check ($admission.admission.reject -eq 'unknown_process_identity') 'BLOCKED: missing-identity precondition not acquired'
    $result=Product 'insertCurrent' @{insertion_id='denied-lifecycle-query';text='MUST NOT WRITE'}
   }finally{$restored=[RemainingBoundaryControl]::RestoreProcessSecurity();Check $restored 'Owned process DACL restoration not confirmed'}
   $recovered=Native 'foreground';$after=Native 'probeClipboardState';$actual=File-Rpc $reuseFolder @{action='state'}
   Check ($result.status -eq 'failed' -and $result.effect -eq 'none' -and $recovered.admission.allowed -and $before.sequence -eq $after.sequence -and $actual.text -ceq 'BEGIN React END' -and $actual.paste_count -eq 0 -and $actual.enter_count -eq 0) 'Unqueryable target accepted or original access not restored'
   return @{unavailable=$admission;result=$result;restored=$restored;recovered=$recovered;actual=$actual;clipboard_unchanged=$true}
  }
  foreach($denyNewOpens in @($false,$true)){
   $caseName=if($denyNewOpens){'R06-inaccessible-input-desktop'}else{'R06-no-foreground'}
   Case $caseName {
    $field=Focus-Field;$null=Native 'probeSeedClipboard';$before=Native 'probeClipboardState'
    try{
     [RemainingBoundaryControl]::EnterEmptyDesktop($denyNewOpens)
     $admission=Native 'foreground';$expected=if($denyNewOpens){'no_interactive_desktop'}else{'no_foreground'}
     Check ($admission.admission.reject -eq $expected) 'BLOCKED: exact private desktop rejection condition not acquired'
     $result=Product 'insertCurrent' @{insertion_id=$caseName;text='MUST NOT WRITE'}
    }finally{$restored=[RemainingBoundaryControl]::RestoreDesktop();Check $restored 'Original desktop restoration not confirmed'}
    $after=Native 'probeClipboardState';$actual=Field 'fieldState'
    Check ($result.status -eq 'failed' -and $result.effect -eq 'none' -and $before.sequence -eq $after.sequence -and $actual.value -ceq 'BEGIN React END' -and $actual.chords.Count -eq 0 -and $actual.enterCount -eq 0) 'Unavailable desktop allowed a target/clipboard side effect'
    return @{admission=$admission;result=$result;actual=$actual;original_desktop_restored=$restored;clipboard_unchanged=$true;scope='own temporary desktop/access control only; not actual Winlogon lock-screen verification'}
   }
  }
  $null=File-Rpc $reuseFolder @{action='quit'};Check ($reuseProcess.WaitForExit(5000)) 'Owned classic window process did not exit'
  Case 'R04-natural-cold-chrome' {
   $chromeExe=@((Join-Path $env:ProgramFiles 'Google/Chrome/Application/chrome.exe'),(Join-Path ${env:ProgramFiles(x86)} 'Google/Chrome/Application/chrome.exe'))|Where-Object{Test-Path $_}|Select-Object -First 1
   Check ([bool]$chromeExe) 'BLOCKED: Chrome executable unavailable'
   $samples=@();$acquired=$false
   foreach($iteration in 1..3){
    $listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0);$listener.Start();$port=$listener.LocalEndpoint.Port;$listener.Stop()
    $page=[Uri]::new((Join-Path $desktop 'native/windows/fixtures/chrome.html')).AbsoluteUri+'?kind=input'
    $chrome=Start-Owned $chromeExe @("--user-data-dir=$(Join-Path $OutputDir "natural-cold-$iteration")",'--no-first-run','--no-default-browser-check','--disable-sync',"--app=$page","--remote-debugging-port=$port",'--remote-debugging-address=127.0.0.1')
    try{
     & node (Join-Path $PSScriptRoot 'chrome-ready.cjs') $port $page 45000 (Join-Path $OutputDir "natural-cold-$iteration-dom.json")
     Check ($LASTEXITCODE -eq 0) 'BLOCKED: natural cold Chrome DOM not ready'
     Check ((Native 'probeFocus' @{fixture_pid=$chrome.Id}).focused) 'BLOCKED: natural cold Chrome foreground not acquired'
     $initial=Native 'probeReadNow' @{fixture_pid=$chrome.Id};$null=Native 'probeSeedClipboard'
     $result=Product 'insertCurrent' @{insertion_id="natural-cold-$iteration";text='中文🙂'}
     $final=Native 'capture' @{fixture_pid=$chrome.Id};$clip=Native 'probeClipboardState'
     $domPath=Join-Path $OutputDir "natural-cold-$iteration-after.json"
     & node (Join-Path $PSScriptRoot 'chrome-ready.cjs') $port $page 5000 $domPath readback
     Check ($LASTEXITCODE -eq 0) 'Independent cold Chrome DOM readback unavailable'
     $dom=(Get-Content -Raw $domPath|ConvertFrom-Json).actual
     $readProperty=$initial.PSObject.Properties['readback'];$readOk=if($null -ne $readProperty){$readProperty.Value.PSObject.Properties['Ok']}else{$null}
     $initiallyUnreadable=($null -eq $readOk -or $null -eq $readOk.Value.caret_utf16)
     $samples+=@{iteration=$iteration;initial=$initial;result=$result;final=$final;dom=$dom;initially_unreadable=$initiallyUnreadable}
     Write-NewJson $samples[-1] (Join-Path $OutputDir "natural-cold-$iteration-observation.json")
     Check ($dom.events.pastes.Count -le 1 -and $dom.events.enters -eq 0 -and $clip.text -ceq 'SVI original clipboard') 'Cold target received repeated input/Enter or clipboard was not restored'
     if($result.status -eq 'inserted'){
      Check ($result.observable -and $result.effect -eq 'written' -and $final.readback.text -ceq 'BEGIN 中文🙂 END' -and $final.readback.caret_utf16 -eq 10 -and $final.readback.selection_utf16_len -eq 0 -and $dom.value -ceq 'BEGIN 中文🙂 END' -and $dom.start -eq 10 -and $dom.end -eq 10 -and $dom.events.pastes.Count -eq 1) 'Cold target claimed inserted without exact independent full-text/caret/selection'
     }else{Check ($result.status -eq 'unverified' -and -not $result.observable -and $result.effect -eq 'unknown') 'Cold missing-pattern result falsely claimed a definitive outcome'}
     if($initiallyUnreadable){$acquired=$true;break}
    }finally{Stop-Owned $chrome}
   }
   Write-NewJson @{samples=$samples;natural_initial_unreadable_acquired=$acquired} (Join-Path $OutputDir 'natural-cold-observations.json')
   Check $acquired 'BLOCKED: all3 natural initial reads were already readable; missing-pattern transition not observed, no fake return substituted'
   return @{samples=$samples;scope='natural fresh Chrome, only DOM readiness before one raw first UIA query; original TC04 allows unknown on missing evidence and inserted only for strict match, no normalization/replay'}
  }
 }else{
 Case 'R10-invalid-fields' {
  $field=Focus-Field;$null=Native 'probeSeedClipboard';$before=Native 'probeClipboardState'
  $bad=@(
   @{action='permission';prompt='true'},@{action='permission';id='1'},@{action='permission';id=1.5},@{action='permission';id=9007199254740992L},
   @{action='validate'},@{action='validate';target=''},@{action='validate';target=('a'*257)},@{action='validate';target=1},
   @{action='release';target=$true},@{action='release';target=@('a')},@{action='capture';restoreFocus=1},
   @{action='insert';target='x';insertion_id='a';text=''},@{action='insert';insertion_id='a';text='x'},
   @{action='insertCurrent';insertion_id='';text='x'},@{action='insertCurrent';insertion_id=('a'*257);text='x'},@{action='insertCurrent';insertion_id=2;text='x'},
   @{action='insertCurrent';insertion_id='a';text=$null},@{action='insertCurrent';insertion_id='a';text=@('x')},@{action='insertCurrent';insertion_id='a';text=('x'*32001)},
   @{action='inputBounds';unknown_field='x'},@{action='unknown'},@{action=2}
  );$rows=@();$i=0
  foreach($body in $bad){$i++;if(-not $body.ContainsKey('id')){$body.id=100000+$i};$responseId=if($body.id -is [string] -or $body.id -is [double] -or $body.id -gt 9007199254740991L){0}else{$body.id};$response=Read-Reply $script:product $body $responseId;Check ($response.error.code -eq 'NATIVE_REQUEST_INVALID' -and $response.error.effect -eq 'none') 'Malformed field reached dispatch';$rows+=@{parameter=$i;input=$body;response=$response}}
  $after=Native 'probeClipboardState';$actual=Field 'fieldState'
  Check ($before.sequence -eq $after.sequence -and $actual.value -ceq 'BEGIN React END' -and $actual.chords.Count -eq 0 -and $actual.enterCount -eq 0) 'Invalid fields caused target or clipboard side effects'
  return @{planned_parameters=$bad.Count;rows=$rows;actual=$actual;clipboard_unchanged=$true}
 }
 Case 'R10-actions' {
  $field=Focus-Field;$capture=Product 'capture';Check ($capture.target -is [string]) 'Capture target unavailable'
  $valid=Product 'validate' @{target=$capture.target;restoreFocus=$false};Check $valid.valid 'Fresh target invalid'
  $bounds=Product 'inputBounds';Check ($bounds.available -and $bounds.coordinateSpace -eq 'physical' -and $bounds.bounds.width -gt 0 -and $bounds.bounds.height -gt 0) 'Input bounds flat contract differs'
  $one=Product 'release' @{target=$capture.target};$two=Product 'release' @{target=$capture.target};$after=Product 'validate' @{target=$capture.target}
  Check ($one.released -and $two.released -and -not $after.valid) 'Release lifecycle not idempotent/invalidated'
  $maxTarget=Product 'release' @{target=('x'*256)};Check $maxTarget.released 'Legal256-byte target was rejected'
  $maxInsertion=Product 'insert' @{target='missing';insertion_id=('x'*256);text='MUST NOT WRITE'};Check ($maxInsertion.status -eq 'failed' -and $maxInsertion.code -eq 'TARGET_CHANGED' -and $maxInsertion.effect -eq 'none') 'Legal256-byte insertion identity did not reach zero-write validation'
  $maximum=Read-Reply $script:product @{id=9007199254740991L;action='permission';prompt=$false};Check $maximum.trusted 'Max safe numeric id rejected'
  return @{capture=$capture;validate=$valid;bounds=$bounds;first_release=$one;second_release=$two;after=$after;max_id=$maximum;max_target=$maxTarget;max_insertion_identity=$maxInsertion}
 }
 Case 'R03-released-target' {
  $field=Focus-Field;$null=Native 'probeSeedClipboard';$capture=Product 'capture';$null=Product 'release' @{target=$capture.target};$before=Native 'probeClipboardState'
  $result=Product 'insert' @{target=$capture.target;insertion_id='released-target';text='MUST NOT WRITE';restoreFocus=$true};$after=Native 'probeClipboardState';$actual=Field 'fieldState'
  Check ($result.status -eq 'failed' -and $result.effect -eq 'none' -and $before.sequence -eq $after.sequence -and $actual.chords.Count -eq 0 -and $actual.value -ceq 'BEGIN React END') 'Released target produced a side effect'
  return @{result=$result;actual=$actual;clipboard_unchanged=$true}
 }
 Case 'R06-disabled-after-capture' {
  try{
  $field=Focus-Field;$capture=Product 'capture';$prepared=Field 'setOwnedPlainDisabled' @{disabled=$true};$info=Native 'inspectPoint' @{fixture_pid=$field.pid;x=$field.point.x;y=$field.point.y}
  Check ($prepared.disabled -and $info.class -eq 'disabled') 'Actual UIA-disabled precondition missing'
  $null=Native 'probeSeedClipboard';$before=Native 'probeClipboardState';$result=Product 'insert' @{target=$capture.target;insertion_id='actual-disabled';text='MUST NOT WRITE';restoreFocus=$true};$after=Native 'probeClipboardState';$actual=Field 'fieldState'
  Check ($result.status -eq 'failed' -and $result.effect -eq 'none' -and $before.sequence -eq $after.sequence -and $actual.fields.plain.value -ceq 'BEGIN React END' -and $actual.chords.Count -eq 0 -and $actual.enterCount -eq 0) 'Disabled captured target accepted or changed clipboard/target'
  return @{actual_disabled=$prepared;uia=$info;result=$result;actual=$actual;clipboard_unchanged=$true}
  }finally{$null=Field 'setOwnedPlainDisabled' @{disabled=$false}}
 }
 Case 'R10-max-utf16' {
  $field=Focus-Field 'editor';$text='🙂'*16000;Check ($text.Length -eq 32000) 'Frozen UTF16 boundary differs';$null=Native 'probeSeedClipboard'
  $result=Product 'insertCurrent' @{insertion_id='max-utf16';text=$text};$actual=Field 'fieldState'
  Check ($result.status -eq 'inserted' -and $result.effect -eq 'written' -and $result.observable -and $actual.value -ceq "BEGIN $text END" -and $actual.start -eq 32006 -and $actual.end -eq 32006 -and $actual.chords.Count -eq 1 -and $actual.enterCount -eq 0) 'Legal32000 UTF16 did not strictly match independent DOM text/caret'
  return @{result=$result;utf16_units=32000;actual_utf16_len=$actual.value.Length;caret=$actual.start;selection=($actual.end-$actual.start);paste_count=$actual.chords.Count;enter_count=$actual.enterCount}
 }
 Case 'R04-notepad-truncated-uia' {
  # The first batch exposed a classic provider returning4096 UTF16 units. Keep
  # that failure; independently read the actual Edit before classifying it.
  $file=Join-Path $OutputDir 'max-utf16.txt';[IO.File]::WriteAllText($file,'BEGIN React END',$utf8)
  $notepad=Start-Owned (Join-Path $env:WINDIR 'System32/notepad.exe') @($file)
  Check ((Native 'probeFocus' @{fixture_pid=$notepad.Id}).focused) 'Owned Notepad unavailable'
  $null=Native 'probeSelect' @{fixture_pid=$notepad.Id;start=6;length=5};$null=Native 'probeSeedClipboard'
  $text='🙂'*16000;Check ($text.Length -eq 32000) 'Frozen UTF16 boundary differs'
  $result=Product 'insertCurrent' @{insertion_id='max-notepad-limited-read';text=$text};$actual=Native 'capture' @{fixture_pid=$notepad.Id};$foreground=Native 'foreground'
  Check ($foreground.root.pid -eq $notepad.Id) 'Owned classic window lost foreground'
  $full=[RemainingWin32]::ReadClassic([IntPtr]::new([long]$foreground.root.hwnd),[uint32]$notepad.Id)
  Check ($full.text -ceq "BEGIN $text END" -and $full.start -eq 32006 -and $full.end -eq 32006) 'Independent Win32 full text/caret differs'
  Check ($actual.readback.text.Length -eq 4096 -and $null -eq $actual.readback.caret_utf16 -and $result.status -eq 'unverified' -and $result.effect -eq 'unknown' -and -not $result.observable) 'Limited UIA read was incorrectly treated as strict success'
  Stop-Owned $notepad;return @{result=$result;utf16_units=32000;independent_text_utf16_len=$full.text.Length;caret=$full.start;selection=($full.end-$full.start);uia_read_utf16_len=$actual.readback.text.Length;scope='known hosted classic UIA truncation; full Win32 read is an independent oracle, not a product fallback'}
 }
 Case 'R09-capacity-10000' {
  $field=Focus-Field;$child=Start-Owned $formal @('--parent-pid',[string]$PID) -Redirect;$err=$child.StandardError.ReadToEndAsync()
  try{
   $first=Read-Reply $child @{id=1;action='insertCurrent';insertion_id='first-written';text='中文🙂'};Check ($first.status -eq 'inserted') 'Capacity sentinel was not actually written'
   $before=Native 'probeClipboardState';$watch=[Diagnostics.Stopwatch]::StartNew();$count=0
   foreach($i in 2..10000){Check ($watch.ElapsedMilliseconds -lt 180000) 'BLOCKED: capacity population exceeded3min; no pass';$reply=Read-Reply $child @{id=$i;action='insert';target='missing';insertion_id="capacity-$i";text='MUST NOT WRITE'};Check ($reply.status -eq 'failed' -and $reply.effect -eq 'none' -and $reply.code -eq 'TARGET_CHANGED') 'Capacity population differs from frozen zero-write oracle';$count++;if($i%1000 -eq 0){Write-NewJson @{identities=$i;planned=10000;elapsed_ms=$watch.ElapsedMilliseconds} (Join-Path $OutputDir "capacity-progress-$i.json")}}
   $full=Read-Reply $child @{id=10001;action='insert';target='missing';insertion_id='overflow';text='MUST NOT WRITE'}
   $again=Read-Reply $child @{id=10002;action='insertCurrent';insertion_id='first-written';text='MUST NOT WRITE'}
   $after=Native 'probeClipboardState';$actual=Field 'fieldState'
   Check ($full.code -eq 'ZERO_WRITE' -and $full.effect -eq 'none' -and $again.status -eq 'unverified' -and $again.effect -eq 'unknown' -and $actual.chords.Count -eq 1 -and $actual.enterCount -eq 0 -and $actual.value -ceq 'BEGIN 中文🙂 END' -and $before.sequence -eq $after.sequence) 'Full execution table evicted/replayed or changed target/clipboard'
   return @{zero_write_population=$count;elapsed_ms=$watch.ElapsedMilliseconds;overflow=$full;old_written_identity=$again;actual=$actual;clipboard_unchanged=$true}
  }finally{Stop-Owned $child;Check ($err.Wait(1000)) 'Capacity helper stderr not closed'}
 }
 foreach($newline in @('lf','crlf')){
  Case "R01-chrome-textarea-$newline" {
   $chrome=@((Join-Path $env:ProgramFiles 'Google/Chrome/Application/chrome.exe'),(Join-Path ${env:ProgramFiles(x86)} 'Google/Chrome/Application/chrome.exe'))|Where-Object{Test-Path $_}|Select-Object -First 1
   Check ([bool]$chrome) 'BLOCKED: owned Chrome unavailable'
   $uri=[Uri]::new((Join-Path $desktop 'native/windows/fixtures/chrome.html')).AbsoluteUri+'?kind=textarea'
   $listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0);$listener.Start();$port=$listener.LocalEndpoint.Port;$listener.Stop()
   $target=Start-Owned $chrome @("--user-data-dir=$(Join-Path $OutputDir "chrome-$newline")",'--no-first-run','--no-default-browser-check','--disable-sync',"--app=$uri","--remote-debugging-port=$port",'--remote-debugging-address=127.0.0.1')
   try{
    & node (Join-Path $PSScriptRoot 'chrome-ready.cjs') $port $uri 45000 (Join-Path $OutputDir "chrome-$newline-ready.json");Check ($LASTEXITCODE -eq 0) 'Cold Chrome DOM not ready'
    Check ((Native 'probeFocus' @{fixture_pid=$target.Id}).focused) 'Chrome foreground not acquired';$null=Native 'probeSelect' @{fixture_pid=$target.Id;start=6;length=5}
    $baseline=Native 'capture' @{fixture_pid=$target.Id};Check ($baseline.readback.text -ceq 'BEGIN React END' -and $baseline.readback.caret_utf16 -eq 6 -and $baseline.readback.selection_utf16_len -eq 5) 'BLOCKED: readable textarea baseline not acquired; this group is newline behavior, not the separate cold-UIA case'
    $text=if($newline -eq 'lf'){"中文🙂`nDocker"}else{"中文🙂`r`nDocker"};$null=Native 'probeSeedClipboard'
    $result=Product 'insertCurrent' @{insertion_id="chrome-$newline";text=$text};$actual=Native 'capture' @{fixture_pid=$target.Id}
    $expected="BEGIN 中文🙂`nDocker END"
    Check ($actual.readback.text -ceq $expected -and $actual.readback.caret_utf16 -eq 17 -and $actual.readback.selection_utf16_len -eq 0) 'Actual textarea newline/caret differs from frozen LF oracle'
    if($newline -eq 'lf'){Check ($result.status -eq 'inserted' -and $result.effect -eq 'written' -and $result.observable) 'Strict LF verification failed'}else{Check ($result.status -eq 'unverified' -and $result.effect -eq 'unknown' -and -not $result.observable) 'CRLF conversion was incorrectly called strict insertion'}
    return @{raw_newline=$newline;result=$result;actual=$actual;expected_raw_lf=$expected}
   }finally{Stop-Owned $target}
  }
 }
 $trayProfile=Join-Path $OutputDir 'tray-profile';$ready=Open-Host 'normal' $trayProfile
 Case 'R23-window-concurrency' {$actual=Host-Rpc 'concurrentWindows';Check (@($actual.ids|Select-Object -Unique).Count -eq 1 -and $actual.state.main.renderer_id -eq $ready.main.renderer_id -and $actual.state.main_window_count -eq 1) 'Production module created duplicate Main windows';return $actual}
 foreach($mode in @('crash','timeout','invalidate')){
  $caseId=switch($mode){'crash'{'R09-bridge-crash'}'timeout'{'R09-bridge-timeout'}default{'R08-bridge-invalidate'}}
  Case $caseId {
   $prepared=Host-Rpc 'bridgePrepare';Check $prepared.permission.trusted 'Real production bridge did not initialize'
   $child=Get-CimInstance Win32_Process -Filter "ProcessId=$($prepared.pid)";Check ($child.ParentProcessId -eq $script:hostProcess.Id -and (Get-FileHash $child.ExecutablePath).Hash -eq (Get-FileHash $formal).Hash) 'Bridge did not launch the frozen formal child'
   $field=Focus-Field;$null=Native 'probeSeedClipboard';$null=Host-Rpc 'bridgeWrite' @{mode=$mode}
   $end=[DateTime]::UtcNow.AddSeconds(3);do{$actual=Field 'fieldState';if($actual.chords.Count -eq 1 -and $actual.value -ceq 'BEGIN 中文🙂 END'){break};Start-Sleep -Milliseconds 20}while([DateTime]::UtcNow -lt $end)
   Check ($actual.chords.Count -eq 1 -and $actual.value -ceq 'BEGIN 中文🙂 END') 'Fault was not acquired after actual target consumption'
   if($mode -eq 'crash'){$killed=Host-Rpc 'bridgeKill';Check $killed.killed 'Owned child was not killed'}elseif($mode -eq 'invalidate'){$null=Host-Rpc 'bridgeInvalidate'}
   $settled=Wait-Host {param($s) $null -ne $s.pending -and $s.pending.status -eq 'rejected'} 9000
   $expectedCode=switch($mode){'crash'{'NATIVE_DISCONNECTED'}'timeout'{'NATIVE_TIMEOUT'}default{'NATIVE_INVALIDATED'}}
   Check ($settled.pending.code -eq $expectedCode -and $settled.pending.effect -eq 'unknown') 'Actual bridge lost write certainty or replayed'
   if($mode -eq 'timeout'){Check ($settled.pending.elapsed_ms -ge 4900 -and $settled.pending.elapsed_ms -lt 8000) 'Five-second bridge deadline differed'}
   $next=Host-Rpc 'bridgeRestart';Check ($next.permission.trusted -and $next.pid -ne $prepared.pid) 'Bridge did not start a distinct helper after failure'
   $final=Field 'fieldState';Check ($final.chords.Count -eq 1 -and $final.enterCount -eq 0 -and $final.value -ceq 'BEGIN 中文🙂 END') 'Fresh helper replayed old work'
   $done=Host-Rpc 'bridgeDone';Check (@($done.children|Where-Object{-not $_.exit_confirmed}).Count -eq 0) 'Owned native child exit was not confirmed'
   return @{prepared=$prepared;pending=$settled.pending;restarted_pid=$next.pid;actual=$final;cleanup=$done.children;scope='real production class/helper; stdout delivery paused after permission as explicit transport fault, no fabricated business result'}
  }
 }
 Case 'R06-bridge-own-window' {
  $prepared=Host-Rpc 'bridgePrepare';$shown=Host-Rpc 'show';Check ((Native 'probeFocus' @{fixture_pid=$shown.pid}).focused) 'Owned integration Main not foreground'
  $null=Native 'probeSeedClipboard';$before=Native 'probeClipboardState';$result=Host-Rpc 'bridgeOwnWindow';$after=Native 'probeClipboardState';$done=Host-Rpc 'bridgeDone'
  Check ($result.result.status -eq 'failed' -and $result.result.effect -eq 'none' -and $before.sequence -eq $after.sequence) 'Actual parent window received a bridge paste/clipboard write'
  return @{result=$result.result;clipboard_unchanged=$true;cleanup=$done.children}
 }
 Case 'R32-production-hud-15' {
  $field=Focus-Field;$before=Native 'foreground';$samples=@()
  foreach($i in 1..15){foreach($state in @('show','move','hide')){$observed=Host-Rpc 'hud' @{state=$state;iteration=$i};$focus=Native 'foreground';Check ($focus.root.hwnd -eq $before.root.hwnd -and $focus.root.pid -eq $before.root.pid -and -not $observed.hud.focused) 'Production HUD stole actual foreground';$samples+=@{iteration=$i;state=$state;hud=$observed.hud;foreground=$focus.root}}}
  Check (-not (Host-Rpc 'state').hud.visible) 'Final production HUD remained visible';return @{cycles=15;observations=$samples;scope='actual production window module and renderer; no recording or multiscreen claim'}
 }
 Case 'R22-first-close-persist' {
  $null=Host-Rpc 'show';$before=Host-Rpc 'state';Check (-not $before.acknowledged) 'First-close profile was not new'
  Close-Main $before;Close-Main $before;$dialog=Dialog;Check ($dialog.dialogs.Count -eq 1) 'First/repeated close did not leave exactly one native notice'
  $waiting=@{main_visible=[RemainingWin32]::IsWindowVisible([IntPtr]::new([long]$before.main.hwnd));ack_file_present=(Test-Path (Join-Path $trayProfile 'tray-state.json'))};Check ($waiting.main_visible -and -not $waiting.ack_file_present) 'Window hid/acknowledged before actual user notice confirmation'
  $ack=Dialog 'acknowledge';$hidden=Wait-Host {param($s) -not $s.main.visible -and $s.acknowledged}
  $null=Host-Rpc 'show';Close-Main;$again=Wait-Host {param($s) -not $s.main.visible};$noDialog=Dialog
  Check ($noDialog.dialogs.Count -eq 0 -and $again.main.renderer_id -eq $before.main.renderer_id -and -not $again.main.background_throttling) 'Second close destroyed/throttled renderer or repeated notice'
  return @{before=$before;native_notice=$dialog;before_ack=$waiting;actual_ack=$ack;first_hidden=$hidden;second_hidden=$again;second_dialog=$noDialog}
 }
 Case 'R23-window-rebuild' {$actual=Host-Rpc 'destroyAndRebuild';Check (@($actual.ids|Select-Object -Unique).Count -eq 1 -and $actual.ids[0] -ne $actual.old_id -and -not $actual.state.main.destroyed -and $actual.state.main_window_count -eq 1) 'Production window module failed unique rebuild';return $actual}
 Case 'R23-renderer-crash-reopen' {$actual=Host-Rpc 'crashAndReopen';$script:fileSequence++;Write-NewJson $actual (Join-Path $OutputDir "renderer-crash-$($script:fileSequence).json");Check ($actual.crashed.is_crashed -and @($actual.ids|Select-Object -Unique).Count -eq 1 -and $actual.ids[0] -ne $actual.crashed.renderer_id -and -not $actual.state.main.renderer_crashed -and $actual.state.main_window_count -eq 1) 'Crashed renderer was reused rather than replaced on actual reopen';return $actual}
 Close-Host
 $restarted=Open-Host 'restart' $trayProfile
 Case 'R22-restart-close' {Check ($script:results['R22-first-close-persist'].status -eq 'pass') 'BLOCKED: first-close acknowledgement precondition failed';Check $restarted.acknowledged 'Close notice not persisted across actual Electron restart';Close-Main;$hidden=Wait-Host {param($s) -not $s.main.visible};$dialog=Dialog;Check ($dialog.dialogs.Count -eq 0) 'Persistent notice repeated after restart';return @{initial=$restarted;hidden=$hidden;dialog=$dialog}}
 Close-Host
 $failure=Open-Host 'persist-failure' (Join-Path $OutputDir 'tray-failure-profile')
 Case 'R22-write-failure' {
  Close-Main;$first=Dialog;Check ($first.dialogs.Count -eq 1) 'Expected actual initial notice';$ack=Dialog 'acknowledge'
  $errorDialog=Dialog;Check ($errorDialog.dialogs.Count -eq 1) 'Actual filesystem failure did not show its recovery dialog';$errorAck=Dialog 'error-acknowledge';$state=Host-Rpc 'state'
  Check ($state.main.visible -and -not $state.acknowledged -and -not $state.ack_file_present) 'Persistence failure hid Main or falsely acknowledged'
  return @{first=$first;actual_ack=$ack;filesystem_error_dialog=$errorDialog;error_ack=$errorAck;final=$state;fault='owned fresh tray-state.json.tmp is an actual directory; write fails, no mocked API'}
 }
 Close-Host
 }
}catch{Write-NewJson @{error=$_.Exception.Message} (Join-Path $OutputDir 'worker-failure.json')}
finally{
 if($Suite -eq 'boundaries'){try{Check ([RemainingBoundaryControl]::RestoreDesktop()) 'Final desktop restoration not confirmed';Check ([RemainingBoundaryControl]::RestoreProcessSecurity()) 'Final process DACL restoration not confirmed'}catch{$script:boundaryCleanupOk=$false;Write-NewJson @{error=$_.Exception.Message} (Join-Path $OutputDir 'boundary-restore-failure.json')}}
 try{$null=Field 'quit'}catch{Write-Warning 'Target fixture quit did not acknowledge'}
 foreach($p in $script:owned){try{Stop-Owned $p}catch{Write-Warning $_.Exception.Message}}
 $log.Dispose();Write-NewJson @($script:cleanup) (Join-Path $OutputDir 'worker-cleanup.json')
 if($nativeErr.Wait(1000)){[IO.File]::WriteAllText((Join-Path $OutputDir 'probe.stderr.txt'),$nativeErr.Result,$utf8)}
 if($productErr.Wait(1000)){[IO.File]::WriteAllText((Join-Path $OutputDir 'formal.stderr.txt'),$productErr.Result,$utf8)}
 $rows=@($script:results.Values);$cleanupOk=(@($script:cleanup|Where-Object{-not $_.exit_confirmed}).Count -eq 0) -and $script:boundaryCleanupOk
 Write-NewJson @{source_sha=$env:GITHUB_SHA;planned=$planned.Count;cases=$rows;cleanup_confirmed=$cleanupOk;scope='supplemental L2 only; original all40/S5 not passed here'} (Join-Path $OutputDir 'remaining-cases.json')
}
if(@($script:results.Values|Where-Object{$_.status -ne 'pass'}).Count -gt 0 -or -not $cleanupOk){exit 1}
exit 0
