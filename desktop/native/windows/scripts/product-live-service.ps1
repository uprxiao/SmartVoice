param([Parameter(Mandatory=$true)][string]$PythonExe,[Parameter(Mandatory=$true)][string]$EvidenceDir,[switch]$Worker)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'probe-files.ps1')
if($env:GITHUB_ACTIONS -ne 'true'){throw 'Owned disposable runner required'}
$verify=Join-Path $env:CARGO_TARGET_DIR 'release/verify.exe'
if(-not $Worker){
    & $verify --medium (Get-Command pwsh).Source -NoProfile -File $PSCommandPath -Worker -PythonExe $PythonExe -EvidenceDir $EvidenceDir
    if($LASTEXITCODE -ne 0){throw 'Actual Main/HTTP/PostgreSQL UI integration failed'}
    exit 0
}
if((& $verify --preflight|ConvertFrom-Json).integrity_rid -ne 8192){throw 'Main/DB integration requires Medium user'}
$repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..'))
$fixture=Join-Path $repo 'server/tests/desktop_live_environment.py'
$ui=Join-Path $PSScriptRoot 'product-live-ui.cjs'
$electron=Join-Path $repo 'desktop/node_modules/electron/dist/electron.exe'
$profileDir=Join-Path $EvidenceDir 'desktop-profile'
$environment=Join-Path $EvidenceDir 'environment.json'
$server=$null;$desktop=$null;$second=$null;$browser=$null;$serverOut=$null;$serverErr=$null
$data=[ordered]@{source_sha=$env:GITHUB_SHA;status='not_run';scope='real Windows Main/preload + real isolated HTTP/PostgreSQL/Admin; mock providers, no hardware/audio/model/S5 pass';desktop_stops=@()}
if(-not ('SviLiveWindows' -as [type])){Add-Type @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class SviLiveWindows {
  public delegate bool Callback(IntPtr hwnd,IntPtr data);
  [DllImport("user32.dll")] static extern bool EnumWindows(Callback cb,IntPtr data);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint pid);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  public static int Visible(uint pid){int count=0;EnumWindows((hwnd,data)=>{uint owner;GetWindowThreadProcessId(hwnd,out owner);if(owner==pid&&IsWindowVisible(hwnd))count++;return true;},IntPtr.Zero);return count;}
}
'@
}
function Assert-Live([bool]$Ok,[string]$Message){if(-not $Ok){throw $Message}}
function Free-Port {
    $listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0);$listener.Start()
    try{return $listener.LocalEndpoint.Port}finally{$listener.Stop()}
}
function Start-Desktop([switch]$Background) {
    $port=Free-Port
    $info=[Diagnostics.ProcessStartInfo]::new($electron);$info.UseShellExecute=$false
    $info.ArgumentList.Add((Join-Path $repo 'desktop'))
    if($Background){$info.ArgumentList.Add('--background')}
    $info.ArgumentList.Add("--remote-debugging-port=$port");$info.ArgumentList.Add('--remote-debugging-address=127.0.0.1')
    $info.Environment['SVI_USER_DATA']=$profileDir
    $process=[Diagnostics.Process]::Start($info)
    return @{process=$process;port=$port}
}
function Stop-Desktop([Diagnostics.Process]$Process){
    $helpers=@(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($Process.Id) AND Name = 'smartvoiceinput-input.exe'")
    if(-not $Process.HasExited){$Process.Kill($true)}
    Assert-Live ($Process.WaitForExit(5000)) 'Owned desktop exit not confirmed'
    $cleanup=@()
    foreach($helper in $helpers){
        $exited=$false
        try{$child=Get-Process -Id $helper.ProcessId -ErrorAction Stop;$exited=$child.WaitForExit(2000)}catch{
            if($_.FullyQualifiedErrorId -like 'NoProcessFoundForGivenId*'){$exited=$true}else{throw}
        }
        $cleanup+=@{exit_confirmed=$exited}
        Assert-Live $exited 'Owned native helper remained alive after Main exit'
    }
    $data.desktop_stops+=@(@{main_exit_confirmed=$true;helpers=$cleanup})
}
function Snapshot([string]$Name){
    $text=& $PythonExe $fixture snapshot --environment $environment
    Assert-Live ($LASTEXITCODE -eq 0) 'Independent database snapshot failed'
    $snapshot=$text|ConvertFrom-Json
    Write-NewJson $snapshot (Join-Path $EvidenceDir $Name)
    return $snapshot
}
try {
    Assert-Live (-not (Test-Path $EvidenceDir)) 'New integration directory required'
    $sources=@{}
    foreach($file in @('server/tests/desktop_live_environment.py','server/app/api/auth.py','server/app/models/user.py','desktop/src/main/core/api-client.ts','desktop/src/main/core/token-store.ts','desktop/src/main/rpc.ts','desktop/native/system-settings.ts','desktop/src/renderer/views/AuthView.tsx','desktop/src/renderer/views/Shell.tsx','desktop/src/renderer/views/SettingsView.tsx','desktop/src/renderer/views/HomeView.tsx','admin/src/api.ts','desktop/native/windows/scripts/product-live-ui.cjs','desktop/native/windows/scripts/product-live-service.ps1','desktop/native/windows/scripts/fixture-acl.ps1')){
        $sources[$file]=(Get-FileHash (Join-Path $repo $file) -Algorithm SHA256).Hash
    }
    Write-NewJson @{source_sha=$env:GITHUB_SHA;frozen_at=[DateTime]::UtcNow.ToString('o');planned=10;case_groups=@{register=5;resume=2;admin=3};oracle='one bootstrap admin, one real desktop registration, 2 Argon2 accounts with learning preference unchanged, 0 audio sessions, logout leaves0 active logins; encrypted local credentials restore across real Main restart';audio='actual denied OS state; no force-grant or audio/model pass';source_hashes=$sources} ($EvidenceDir+'.plan.json')
    Assert-Live (Test-Path $env:PGBIN) 'Preinstalled frozen PostgreSQL tools unavailable'
    & $PythonExe $fixture prepare --run-dir $EvidenceDir --pg-bin $env:PGBIN --port 17863
    Assert-Live ($LASTEXITCODE -eq 0) 'Isolated fixture setup failed; private setup evidence retained'
    [IO.Directory]::CreateDirectory($profileDir)|Out-Null
    $before=Snapshot 'database-before.json'
    Assert-Live ($before.users.Count -eq 1 -and $before.sessions -eq 0 -and $before.active_logins -eq 0) 'Database baseline is not the frozen empty fixture'
    $info=[Diagnostics.ProcessStartInfo]::new($PythonExe);$info.UseShellExecute=$false;$info.CreateNoWindow=$true
    foreach($arg in @($fixture,'serve','--environment',$environment)){$info.ArgumentList.Add($arg)}
    $info.WorkingDirectory=Join-Path $repo 'server';$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
    $server=[Diagnostics.Process]::Start($info);$serverOut=$server.StandardOutput.ReadToEndAsync();$serverErr=$server.StandardError.ReadToEndAsync()
    $end=[DateTime]::UtcNow.AddSeconds(25);$ready=$false
    while([DateTime]::UtcNow -lt $end){
        if($server.HasExited){throw 'Owned API exited during startup'}
        try{if((Invoke-WebRequest 'http://127.0.0.1:17863/health/ready' -TimeoutSec 2).StatusCode -eq 200){$ready=$true;break}}catch{}
        Start-Sleep -Milliseconds 100
    }
    Assert-Live $ready 'Owned API readiness deadline exceeded'
    $listeners=@(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 17863 -State Listen)
    $runtime=Get-Content -Raw (Join-Path $EvidenceDir 'runtime-owner.json')|ConvertFrom-Json
    $prepared=Get-Content -Raw (Join-Path $EvidenceDir 'prepared.json')|ConvertFrom-Json
    $runtimeProcess=Get-Process -Id $runtime.pid -ErrorAction Stop
    $runtimeFacts=Get-CimInstance Win32_Process -Filter "ProcessId = $($runtime.pid)"
    $direct=($runtime.pid -eq $server.Id)
    $child=($runtime.parent_pid -eq $server.Id -and $runtimeFacts.ParentProcessId -eq $server.Id)
    $owner=@{listener_count=$listeners.Count;launcher_pid=$server.Id;runtime_pid=$runtime.pid;direct_launcher=$direct;direct_owned_child=$child;runtime_not_older=($runtimeProcess.StartTime -ge $server.StartTime);binary_hash_matches=($runtime.binary_sha256 -eq $prepared.python_binary_sha256 -and (Get-FileHash -LiteralPath $runtimeFacts.ExecutablePath).Hash -eq $prepared.python_binary_sha256);listener_matches_runtime=($listeners.Count -eq 1 -and $listeners[0].OwningProcess -eq $runtime.pid);launcher_alive=(-not $server.HasExited)}
    Write-NewJson $owner (Join-Path $EvidenceDir 'api-owner.json')
    # Windows venv python.exe may be a redirector. Accept only this exact owned
    # process or its direct, independently identified Python runtime child.
    Assert-Live (($owner.direct_launcher -or $owner.direct_owned_child) -and $owner.runtime_not_older -and $owner.binary_hash_matches -and $owner.listener_matches_runtime -and $owner.launcher_alive) 'Loopback API listener does not belong to the owned Python runtime'
    $data.api_listener_owned=$true
    $native=Join-Path $env:CARGO_TARGET_DIR 'x86_64-pc-windows-msvc/release/smartvoiceinput-input.exe'
    $packagedHelper=Join-Path $env:RUNNER_TEMP 'SVI 产品 smoke/win-unpacked/resources/voice/smartvoiceinput-input.exe'
    $devHelper=Join-Path $repo 'desktop/resources/voice/smartvoiceinput-input.exe'
    if(-not (Test-Path $devHelper)){
        [IO.Directory]::CreateDirectory((Split-Path $devHelper -Parent))|Out-Null
        [IO.File]::Copy($native,$devHelper,$false)
    }
    $helperIdentity=@{build_profile='x86_64-pc-windows-msvc/+crt-static';probe_sha256=(Get-FileHash $native).Hash;development_sha256=(Get-FileHash $devHelper).Hash;packaged_sha256=(Get-FileHash $packagedHelper).Hash}
    Write-NewJson $helperIdentity (Join-Path $EvidenceDir 'helper-identity.json')
    Assert-Live ($helperIdentity.probe_sha256 -eq $helperIdentity.development_sha256 -and $helperIdentity.probe_sha256 -eq $helperIdentity.packaged_sha256) 'Probe, development and packaged static CRT helpers differ'
    $data.helper_identity=$helperIdentity
    $launch=Start-Desktop;$desktop=$launch.process
    & node $ui $launch.port $environment (Join-Path $EvidenceDir 'desktop-register.json') 'register'
    Assert-Live ($LASTEXITCODE -eq 0) 'Actual desktop register/Windows permission UI cases failed'
    $registered=Snapshot 'database-after-register.json'
    Assert-Live ($registered.users.Count -eq 2 -and $registered.sessions -eq 0 -and $registered.active_logins -eq 1 -and @($registered.users|Where-Object{-not $_.argon2id -or -not $_.learning_enabled}).Count -eq 0) 'Real account/Argon2/learning preference/session count differs'
    $auth=@(Get-ChildItem $profileDir -Filter 'auth-*.bin' -File)
    Assert-Live ($auth.Count -eq 1 -and $auth[0].Length -gt 64) 'Actual Windows encrypted credential file missing'
    $cipher=[IO.File]::ReadAllBytes($auth[0].FullName);$text=[Text.Encoding]::UTF8.GetString($cipher)
    $parseable=$false;try{$null=$text|ConvertFrom-Json;$parseable=$true}catch{}
    $private=Get-Content -Raw (Join-Path $EvidenceDir 'credentials.private.json')|ConvertFrom-Json
    Assert-Live (-not $parseable -and -not $text.Contains($private.password) -and $text -notmatch 'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+') 'Credentials were persisted as plaintext'
    $data.credential_persistence=@{file_count=$auth.Count;bytes=$cipher.Length;parseable_json=$parseable;plaintext_found=$false;sha256=(Get-FileHash $auth[0].FullName).Hash}
    Stop-Desktop $desktop;$desktop=$null
    $foregroundBefore=[SviLiveWindows]::GetForegroundWindow().ToInt64()
    $launch=Start-Desktop -Background;$desktop=$launch.process
    & node $ui $launch.port $environment (Join-Path $EvidenceDir 'desktop-resume.json') 'boot'
    Assert-Live ($LASTEXITCODE -eq 0) 'Actual background credential restore UI failed'
    $data.saved_account_background=@{visible_count=[SviLiveWindows]::Visible($desktop.Id);foreground_unchanged=([SviLiveWindows]::GetForegroundWindow().ToInt64() -eq $foregroundBefore)}
    Assert-Live ($data.saved_account_background.visible_count -eq 0 -and $data.saved_account_background.foreground_unchanged) 'Saved-account background restore activated a window'
    $second=Start-Desktop
    Assert-Live ($second.process.WaitForExit(10000) -and $second.process.ExitCode -eq 0) 'Saved-account second instance did not exit'
    $end=[DateTime]::UtcNow.AddSeconds(10)
    while([DateTime]::UtcNow -lt $end -and [SviLiveWindows]::Visible($desktop.Id) -ne 1){Start-Sleep -Milliseconds 100}
    Assert-Live ([SviLiveWindows]::Visible($desktop.Id) -eq 1) 'Second instance did not show the same restored account window'
    $data.saved_account_second_instance=@{exit_code=$second.process.ExitCode;original_visible_count=[SviLiveWindows]::Visible($desktop.Id);original_process_alive=(-not $desktop.HasExited)}
    & node $ui $launch.port $environment (Join-Path $EvidenceDir 'desktop-logout.json') 'logout'
    Assert-Live ($LASTEXITCODE -eq 0) 'Actual restored-account logout UI failed'
    Stop-Desktop $desktop;$desktop=$null
    $loggedOut=Snapshot 'database-after-desktop-logout.json'
    Assert-Live ($loggedOut.active_logins -eq 0 -and $loggedOut.sessions -eq 0 -and @(Get-ChildItem $profileDir -Filter 'auth-*.bin' -File).Count -eq 0) 'Logout left server session or local credentials'
    $port=Free-Port;$chromeProfile=Join-Path $EvidenceDir 'admin-browser-profile'
    $browserInfo=[Diagnostics.ProcessStartInfo]::new($env:SVI_PROBE_CHROME);$browserInfo.UseShellExecute=$false
    foreach($arg in @("--user-data-dir=$chromeProfile",'--headless=new','--no-first-run','--disable-sync',"--remote-debugging-port=$port",'--remote-debugging-address=127.0.0.1','http://127.0.0.1:17863/admin/')){$browserInfo.ArgumentList.Add($arg)}
    $browser=[Diagnostics.Process]::Start($browserInfo)
    & node $ui $port $environment (Join-Path $EvidenceDir 'admin-ui.json') 'admin'
    Assert-Live ($LASTEXITCODE -eq 0) 'Admin same-database real UI cases failed'
    $final=Snapshot 'database-final.json'
    Assert-Live ($final.users.Count -eq 2 -and $final.active_logins -eq 0 -and $final.sessions -eq 0 -and @($final.users|Where-Object{-not $_.learning_enabled}).Count -eq 0) 'Final real database differs from the independent oracle'
    $data.status='pass'
} catch {$data.status='fail';$data.error=$_.Exception.Message;throw}
finally {
    if($null -ne $second -and -not $second.process.HasExited){try{Stop-Desktop $second.process}catch{$data.second_cleanup_error=$_.Exception.Message;$data.status='fail'}}
    if($null -ne $desktop){try{Stop-Desktop $desktop}catch{$data.desktop_cleanup_error=$_.Exception.Message;$data.status='fail'}}
    if($null -ne $browser){if(-not $browser.HasExited){$browser.Kill($true)};$data.browser_exit_confirmed=$browser.WaitForExit(5000);if(-not $data.browser_exit_confirmed){$data.status='fail'}}
    if($null -ne $server){
        if(-not $server.HasExited){$server.Kill($true)};$data.server_exit_confirmed=$server.WaitForExit(5000)
        if(-not $data.server_exit_confirmed){$data.status='fail'}
        if($serverOut.Wait(1000)){[IO.File]::WriteAllText((Join-Path $EvidenceDir 'server-stdout.private.log'),$serverOut.Result)}
        if($serverErr.Wait(1000)){[IO.File]::WriteAllText((Join-Path $EvidenceDir 'server-stderr.private.log'),$serverErr.Result)}
        & $PythonExe $fixture revoke --environment $environment
        $data.login_cleanup_confirmed=($LASTEXITCODE -eq 0)
        if(-not $data.login_cleanup_confirmed){$data.status='fail'}
    }
    if(Test-Path $environment){& $PythonExe $fixture stop-pg --environment $environment;$data.postgres_cleanup_confirmed=($LASTEXITCODE -eq 0);if(-not $data.postgres_cleanup_confirmed){$data.status='fail'}}
    if(Test-Path $EvidenceDir){Write-NewJson $data (Join-Path $EvidenceDir 'summary.json')}
    if($data.status -ne 'pass'){throw 'Actual client/service integration failed; all acquired evidence retained'}
}
