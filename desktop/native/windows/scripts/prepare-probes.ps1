param([Parameter(Mandatory=$true)][string]$EnvironmentFile)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Preparation is restricted to a disposable GitHub runner' }

$chrome = @(
    (Join-Path $env:ProgramFiles 'Google/Chrome/Application/chrome.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Google/Chrome/Application/chrome.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { throw 'Preflight blocked: Chrome not installed on this runner image' }
$code = @(
    (Join-Path $env:ProgramFiles 'Microsoft VS Code/Code.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs/Microsoft VS Code/Code.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $code) {
    $archive = Join-Path $env:RUNNER_TEMP 'svi-vscode.zip'
    $directory = Join-Path $env:RUNNER_TEMP 'svi-vscode'
    Invoke-WebRequest 'https://update.code.visualstudio.com/latest/win32-x64-archive/stable' -OutFile $archive
    Expand-Archive $archive $directory
    $code = Join-Path $directory 'Code.exe'
}
$fixture = Join-Path $env:SVI_PROBE_BUNDLE 'win-unpacked/SVI Windows Probe.exe'
if (-not (Test-Path $fixture)) { throw 'Packaged Electron probe missing' }
$helper = Join-Path $env:CARGO_TARGET_DIR 'release/smartvoiceinput-input.exe'
$packagedHelper = Join-Path $env:SVI_PROBE_BUNDLE 'win-unpacked/resources/voice/smartvoiceinput-input.exe'
if ((Get-FileHash $helper -Algorithm SHA256).Hash -ne (Get-FileHash $packagedHelper -Algorithm SHA256).Hash) {
    throw 'Packaged helper does not match the compiled probe'
}
$root = (Get-Location).Path
$files = @(Get-ChildItem 'desktop/native/windows' -Recurse -File | Where-Object { $_.FullName -notmatch '[\\/]target[\\/]' } | ForEach-Object {
    @{path=$_.FullName.Substring($root.Length + 1);sha256=(Get-FileHash $_.FullName -Algorithm SHA256).Hash}
})
$environment = @{
    frozen_at=[DateTime]::UtcNow.ToString('o');source_sha=(& git rev-parse HEAD)
    runner_os=$env:RUNNER_OS;runner_arch=$env:RUNNER_ARCH
    image_os=$env:ImageOS;image_version=$env:ImageVersion
    session_id=[System.Diagnostics.Process]::GetCurrentProcess().SessionId
    windows=[System.Environment]::OSVersion.VersionString;powershell=$PSVersionTable.PSVersion.ToString()
    rustc=(& rustc --version);cargo=(& cargo --version)
    chrome=@{file=$chrome;version=[System.Diagnostics.FileVersionInfo]::GetVersionInfo($chrome).FileVersion;sha256=(Get-FileHash $chrome -Algorithm SHA256).Hash}
    vscode=@{file=$code;version=[System.Diagnostics.FileVersionInfo]::GetVersionInfo($code).FileVersion;sha256=(Get-FileHash $code -Algorithm SHA256).Hash}
    fixture=@{file=$fixture;sha256=(Get-FileHash $fixture -Algorithm SHA256).Hash}
    helper=@{sha256=(Get-FileHash $helper -Algorithm SHA256).Hash;packaged_sha256=(Get-FileHash $packagedHelper -Algorithm SHA256).Hash}
    planned=15;blocking_planned=9;inputs=$files;data='fictional fixtures only; no account, audio, model or service calls'
}
$environment | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $EnvironmentFile -Encoding utf8NoBOM -NoClobber
"SVI_PROBE_CHROME=$chrome" | Out-File $env:GITHUB_ENV -Encoding utf8 -Append
"SVI_PROBE_CODE=$code" | Out-File $env:GITHUB_ENV -Encoding utf8 -Append
"SVI_PROBE_FIXTURE=$fixture" | Out-File $env:GITHUB_ENV -Encoding utf8 -Append
