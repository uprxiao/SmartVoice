param([Parameter(Mandatory=$true)][string]$EvidenceDir,[switch]$Worker)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'probe-files.ps1')
if($env:GITHUB_ACTIONS -ne 'true'){throw 'Disposable owned runner required'}
$verify=Join-Path $env:CARGO_TARGET_DIR 'release/verify.exe'
if(-not $Worker){
    & $verify --medium (Get-Command pwsh).Source -NoProfile -File $PSCommandPath -Worker -EvidenceDir $EvidenceDir
    if($LASTEXITCODE -ne 0){throw 'Credential controls did not complete'}
    exit 0
}
function Assert-Live([bool]$Ok,[string]$Message){if(-not $Ok){throw $Message}}
$repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..'))
$electron=Join-Path $repo 'desktop/node_modules/electron/dist/electron.exe'
function Crypto-Control {
    $records=@()
    foreach($variant in @(@{kind='hard';api='sync'},@{kind='graceful';api='sync'},@{kind='hard';api='async'},@{kind='hard';api='sync-flush'},@{kind='hard';api='sync-prewarmed'})){
        $kind=$variant.kind;$apiMode=$variant.api
        $directory=Join-Path $EvidenceDir "crypto-$apiMode-$kind"
        Assert-Live (-not (Test-Path $directory)) 'Fresh crypto control required'
        [IO.Directory]::CreateDirectory($directory)|Out-Null
        Write-NewJson @{schema='svi-safe-storage-control-v1';exit_kind=$kind;api_mode=$apiMode} (Join-Path $directory 'owner.json')
        $row=@{exit_kind=$kind;api_mode=$apiMode;scope='standalone real safeStorage control, not product or UI pass'}
        $phases=if($apiMode -eq 'sync-prewarmed'){@('warmup','encrypt','decrypt')}else{@('encrypt','decrypt')}
        foreach($phase in $phases){
            $process=$null
            try{
                $info=[Diagnostics.ProcessStartInfo]::new($electron);$info.UseShellExecute=$false;$info.CreateNoWindow=$true
                $info.Environment.Clear()
                foreach($key in @('PATH','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','APPDATA','LOCALAPPDATA','HOMEDRIVE','HOMEPATH','GITHUB_ACTIONS','RUNNER_TEMP')){if($null -ne [Environment]::GetEnvironmentVariable($key)){$info.Environment[$key]=[Environment]::GetEnvironmentVariable($key)}}
                foreach($arg in @((Join-Path $repo 'desktop/native/windows/fixtures/credential-control.cjs'),"--user-data-dir=$directory",'--control-dir',$directory,'--control-phase',$phase,'--control-exit',$kind,'--control-api',$apiMode)){$info.ArgumentList.Add($arg)}
                $process=[Diagnostics.Process]::Start($info)
                $file=Join-Path $directory "$phase.json";$end=[DateTime]::UtcNow.AddSeconds(8)
                while(-not (Test-Path $file)){
                    if($process.HasExited -or [DateTime]::UtcNow -ge $end){throw 'Standalone crypto control did not produce its own metadata'}
                    Start-Sleep -Milliseconds 20
                }
                $row[$phase]=Get-Content -Raw $file|ConvertFrom-Json
                if($phase -eq 'encrypt' -and $kind -eq 'hard' -and -not $process.HasExited){$process.Kill($true)}
                Assert-Live ($process.WaitForExit(5000)) 'Standalone crypto process exit not confirmed'
                $row["${phase}_exit_confirmed"]=$true
            }catch{$row["${phase}_error"]=$_.Exception.Message}
            finally{if($null -ne $process -and -not $process.HasExited){$process.Kill($true);$row["${phase}_forced_cleanup_confirmed"]=$process.WaitForExit(2000)}}
        }
        $records+=@($row)
    }
    Write-NewJson @{scope='five independently owned actual Electron encryption controls; no token/key/cipher contents';observations=$records} (Join-Path $EvidenceDir 'crypto-control.json')
}
$data=@{source_sha=$env:GITHUB_SHA;scope='five standalone real Electron safeStorage controls; no account, product gate or S5 pass';planned=5;status='not_run'}
try{
    Assert-Live ((& $verify --preflight|ConvertFrom-Json).integrity_rid -eq 8192) 'Controls require the actual Medium user'
    Assert-Live (-not (Test-Path $EvidenceDir) -and [IO.Path]::GetFullPath((Split-Path $EvidenceDir -Parent)) -eq [IO.Path]::GetFullPath($env:RUNNER_TEMP)) 'Fresh direct child of owned RUNNER_TEMP required'
    [IO.Directory]::CreateDirectory($EvidenceDir)|Out-Null
    $hashes=@{}
    foreach($name in @('desktop/native/windows/fixtures/credential-control.cjs','desktop/native/windows/scripts/credential-controls.ps1','desktop/native/windows/src/launch.rs','.github/workflows/windows-credentials.yml')){$hashes[$name]=(Get-FileHash (Join-Path $repo $name)).Hash}
    Write-NewJson @{source_sha=$env:GITHUB_SHA;frozen_at=[DateTime]::UtcNow.ToString('o');source_hashes=$hashes;planned=5;parameters=@('sync-hard','sync-graceful','async-hard','sync-flush-hard','sync-prewarmed-hard');expected_known=@{sync_hard='decrypt error';sync_graceful='matches';async_hard='decrypt error'};candidates='public DOMStorage flush and independent graceful prewarm not yet adopted';preparation_ms=8000;exit_confirmation_ms=5000;plaintext='fixed fictional canary only; no account or provider';runtime_sha256=(Get-FileHash $electron).Hash} (Join-Path $EvidenceDir 'plan.json')
    Crypto-Control
    $observations=(Get-Content -Raw (Join-Path $EvidenceDir 'crypto-control.json')|ConvertFrom-Json).observations
    Assert-Live ($observations.Count -eq 5) 'Incomplete control denominator'
    foreach($row in $observations){
        foreach($phase in $(if($row.api_mode -eq 'sync-prewarmed'){@('warmup','encrypt','decrypt')}else{@('encrypt','decrypt')})){
            Assert-Live ($null -ne $row.PSObject.Properties[$phase] -and $row.PSObject.Properties["${phase}_exit_confirmed"].Value) 'Control observation or owned process cleanup missing'
        }
    }
    $data.status='execution_complete';$data.observations=$observations
}catch{$data.status='execution_failed';$data.error=$_.Exception.Message;throw}
finally{if(Test-Path $EvidenceDir){Write-NewJson $data (Join-Path $EvidenceDir 'summary.json')}}
