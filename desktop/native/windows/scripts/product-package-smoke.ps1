param([Parameter(Mandatory=$true)][string]$PackageDir,[Parameter(Mandatory=$true)][string]$EvidenceFile,[switch]$Worker)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'probe-files.ps1')
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Package smoke is restricted to a disposable runner' }
$verify=Join-Path $env:CARGO_TARGET_DIR 'release/verify.exe'
if (-not $Worker) {
    & $verify --medium (Get-Command pwsh).Source -NoProfile -File $PSCommandPath -Worker -PackageDir $PackageDir -EvidenceFile $EvidenceFile
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $EvidenceFile)) { throw 'Packaged product Medium smoke failed' }
    exit 0
}
$preflight=& $verify --preflight | ConvertFrom-Json
if ($preflight.integrity_rid -ne 8192) { throw 'Actual package smoke must run at Medium integrity' }
Add-Type @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class SviProductWindows {
  public delegate bool Callback(IntPtr hwnd, IntPtr param);
  [DllImport("user32.dll")] static extern bool EnumWindows(Callback cb, IntPtr param);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint pid);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  public static long[] Visible(uint pid) {
    var result=new List<long>();
    EnumWindows((hwnd,param)=>{uint owner;GetWindowThreadProcessId(hwnd,out owner);if(owner==pid && IsWindowVisible(hwnd))result.Add(hwnd.ToInt64());return true;},IntPtr.Zero);
    return result.ToArray();
  }
}
'@
$data=[ordered]@{source_sha=$env:GITHUB_SHA;integrity_rid=$preflight.integrity_rid;scope='actual packaged app/bridge; no login, audio, model, hardware or S5';status='not_run'}
$root=$null
$second=$null
$helpers=@()
try {
    $exe=Join-Path $PackageDir 'win-unpacked/SmartVoiceInput.exe'
    $voice=Join-Path $PackageDir 'win-unpacked/resources/voice'
    $resources=@(Get-ChildItem $voice -File)
    if ($resources.Count -ne 1 -or $resources[0].Name -ne 'smartvoiceinput-input.exe') { throw 'Unexpected helper/probe files in product package' }
    $data.helper_sha256=(Get-FileHash $resources[0].FullName -Algorithm SHA256).Hash
    $vswhere=Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
    $installation=& $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    $tools=Get-ChildItem (Join-Path $installation 'VC/Tools/MSVC') -Directory | Sort-Object Name -Descending | Select-Object -First 1
    $dumpbin=Join-Path $tools.FullName 'bin/Hostx64/x64/dumpbin.exe'
    $dependencies=& $dumpbin /DEPENDENTS $resources[0].FullName
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect native helper imports' }
    $dlls=@($dependencies | ForEach-Object { if ($_ -match '^\s+([a-zA-Z0-9_.-]+\.dll)\s*$') { $Matches[1] } })
    if ($dlls.Count -eq 0 -or @($dlls | Where-Object { $_ -match '(?i)vcruntime|msvcp|msvcr|ucrtbase|api-ms-win-crt' }).Count -gt 0) { throw 'Product helper still depends on an external C runtime' }
    $data.helper_imports=$dlls
    $data.installer=@(Get-ChildItem $PackageDir -Filter 'SmartVoiceInput-*.exe' -File | ForEach-Object { @{name=$_.Name;sha256=(Get-FileHash $_.FullName -Algorithm SHA256).Hash} })
    if ($data.installer.Count -ne 1) { throw 'Expected one actual NSIS installer' }
    $before=[SviProductWindows]::GetForegroundWindow().ToInt64()
    $start=[System.Diagnostics.ProcessStartInfo]::new($exe)
    $start.UseShellExecute=$false
    $start.ArgumentList.Add('--background')
    $listener=[System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback,0)
    $listener.Start()
    $debugPort=$listener.LocalEndpoint.Port
    $listener.Stop()
    $start.ArgumentList.Add("--remote-debugging-port=$debugPort")
    $start.ArgumentList.Add('--remote-debugging-address=127.0.0.1')
    $start.Environment['SVI_USER_DATA']=Join-Path $env:RUNNER_TEMP "svi-product-user-$env:GITHUB_RUN_ID"
    $root=[System.Diagnostics.Process]::Start($start)
    $end=[DateTime]::UtcNow.AddSeconds(25)
    do {
        if ($root.HasExited) { throw 'Background product exited during initialization' }
        $helpers=@(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($root.Id) AND Name = 'smartvoiceinput-input.exe'")
        if ($helpers.Count -gt 0) { break }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $end)
    if ($helpers.Count -eq 0) { throw 'Real Main/Renderer app.info never started the production helper' }
    $visible=@([SviProductWindows]::Visible($root.Id))
    $foreground=[SviProductWindows]::GetForegroundWindow().ToInt64()
    if ($visible.Count -ne 0 -or $foreground -ne $before) { throw 'Background startup showed/activated a window' }
    foreach ($helper in $helpers) {
        if ([IO.Path]::GetFullPath($helper.ExecutablePath) -ine [IO.Path]::GetFullPath($resources[0].FullName)) { throw 'Main started an unexpected helper binary' }
        if ($helper.CommandLine -notmatch "--parent-pid\s+$($root.Id)(?:\s|$)") { throw 'Main omitted its parent identity' }
    }
    $data.background=@{visible_count=$visible.Count;foreground_unchanged=($before -eq $foreground);helpers=$helpers.Count;real_bridge=$true}
    $rpcEvidence=Join-Path (Split-Path $EvidenceFile -Parent) 'product-rpc.json'
    & node (Join-Path $PSScriptRoot 'product-rpc-smoke.cjs') $debugPort $rpcEvidence
    if ($LASTEXITCODE -ne 0) {
        $folder=Join-Path (Split-Path $EvidenceFile -Parent) 'startup-diagnostics'
        [IO.Directory]::CreateDirectory($folder)|Out-Null
        $fixtureInfo=[System.Diagnostics.ProcessStartInfo]::new($env:SVI_PROBE_FIXTURE)
        $fixtureInfo.UseShellExecute=$false
        $fixtureInfo.ArgumentList.Add('--probe-dir');$fixtureInfo.ArgumentList.Add($folder)
        $diagnostic=[System.Diagnostics.Process]::Start($fixtureInfo)
        try {
            $until=[DateTime]::UtcNow.AddSeconds(15)
            while (-not (Test-Path (Join-Path $folder 'ready.json'))) {
                if([DateTime]::UtcNow -ge $until){throw 'Startup diagnostic fixture timeout'}
                Start-Sleep -Milliseconds 50
            }
            Write-NewJson @{action='startup-diagnostics';productExe=$exe} (Join-Path $folder '1.request.tmp')
            [IO.File]::Move((Join-Path $folder '1.request.tmp'),(Join-Path $folder '1.request.json'))
            while (-not (Test-Path (Join-Path $folder '1.response.json'))) {
                if([DateTime]::UtcNow -ge $until){throw 'Startup diagnostic response timeout'}
                Start-Sleep -Milliseconds 50
            }
            $raw=Get-Content -Raw (Join-Path $folder '1.response.json')|ConvertFrom-Json
            Write-NewJson $raw (Join-Path (Split-Path $EvidenceFile -Parent) 'product-startup-diagnostics.json')
        } finally {if(-not $diagnostic.HasExited){$diagnostic.Kill($true);$diagnostic.WaitForExit(3000)|Out-Null}}
        throw 'Actual packaged local RPC/system startup test failed; independent diagnostic preserved'
    }
    $secondInfo=[System.Diagnostics.ProcessStartInfo]::new($exe)
    $secondInfo.UseShellExecute=$false
    $secondInfo.Environment['SVI_USER_DATA']=$start.Environment['SVI_USER_DATA']
    $second=[System.Diagnostics.Process]::Start($secondInfo)
    if (-not $second.WaitForExit(10000)) { throw 'Second instance remained alive' }
    $end=[DateTime]::UtcNow.AddSeconds(10)
    do {
        $visible=@([SviProductWindows]::Visible($root.Id))
        if ($visible.Count -eq 1) { break }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $end)
    if ($root.HasExited -or $visible.Count -ne 1) { throw 'Second instance did not open the existing main window' }
    $data.second_instance=@{exit_code=$second.ExitCode;visible_count=$visible.Count;original_process_alive=(-not $root.HasExited)}
    $data.status='pass'
} catch { $data.status='fail';$data.error=$_.Exception.Message;throw }
finally {
    foreach ($process in @($second,$root)) {
        if ($null -ne $process -and -not $process.HasExited) { $process.Kill($true);$process.WaitForExit(5000)|Out-Null }
    }
    $cleanup=@()
    foreach ($helper in $helpers) {
        $ended=$false
        try { $process=Get-Process -Id $helper.ProcessId -ErrorAction Stop;$ended=$process.WaitForExit(2000) } catch { $ended=$true }
        $cleanup+=@{pid=$helper.ProcessId;exit_confirmed=$ended}
    }
    $data.cleanup=$cleanup
    $cleanupFailed=@($cleanup | Where-Object { -not $_.exit_confirmed }).Count -gt 0
    if ($cleanupFailed) { $data.status='fail';$data.cleanup_error='helper termination not confirmed' }
    Write-NewJson $data $EvidenceFile
    if ($cleanupFailed) { throw 'Product helper cleanup not confirmed' }
}
