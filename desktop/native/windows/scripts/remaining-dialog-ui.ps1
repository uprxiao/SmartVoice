param(
    [Parameter(Mandatory=$true)][int]$TargetPid,
    [Parameter(Mandatory=$true)][int]$OwnerPid,
    [Parameter(Mandatory=$true)][string]$ExpectedExeSha256,
    [Parameter(Mandatory=$true)][string]$OutputFile,
    [Parameter(Mandatory=$true)][string]$NativeDll,
    [Parameter(Mandatory=$true)][string]$ExpectedNativeDllSha256,
    [ValidateSet('inspect','acknowledge','error-acknowledge')][string]$Action='inspect'
)
# Run with Windows PowerShell 5.1 for the installed .NET UIAutomation assemblies.
$ErrorActionPreference='Stop'
if($env:GITHUB_ACTIONS -ne 'true' -or $ExpectedExeSha256 -notmatch '^[a-fA-F0-9]{64}$'){throw 'Disposable CI UI observer required'}
$process=Get-Process -Id $TargetPid -ErrorAction Stop
$facts=Get-CimInstance Win32_Process -Filter "ProcessId=$TargetPid"
$hasher=[Security.Cryptography.SHA256]::Create()
try{$actualHash=[BitConverter]::ToString($hasher.ComputeHash([IO.File]::ReadAllBytes($process.Path))).Replace('-','')}finally{$hasher.Dispose()}
if($facts.ParentProcessId -ne $OwnerPid -or $actualHash -ne $ExpectedExeSha256){throw 'Refusing a dialog outside the exact owned Electron process'}
$hasher=[Security.Cryptography.SHA256]::Create()
try{$dllHash=[BitConverter]::ToString($hasher.ComputeHash([IO.File]::ReadAllBytes($NativeDll))).Replace('-','')}finally{$hasher.Dispose()}
if($dllHash -ne $ExpectedNativeDllSha256){throw 'Owned native dialog observer hash mismatch'}
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
[void][Reflection.Assembly]::LoadFrom($NativeDll)
$root=[System.Windows.Automation.AutomationElement]::RootElement
$pidCondition=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty,$TargetPid)
$elements=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,$pidCondition)
$dialogs=@()
foreach($element in $elements){
    if($element.Current.ClassName -eq '#32770'){
        $condition=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Button)
        $buttons=$element.FindAll([System.Windows.Automation.TreeScope]::Descendants,$condition)
        $hwnd=$element.Current.NativeWindowHandle
        $dialogs+=@{element=$element;hwnd=$hwnd;buttons=@($buttons|ForEach-Object{$_.Current.Name});native_children=@([OwnedDialogNative]::Children($hwnd,[uint32]$TargetPid))}
    }
}
$report=@{action=$Action;dialogs=@($dialogs|ForEach-Object{@{hwnd=$_.hwnd;buttons=$_.buttons;native_children=$_.native_children}});invoked=$false}
$ackLabel=([string][char]0x77E5)+[char]0x9053+[char]0x4E86
$okLabel=([string][char]0x786E)+[char]0x5B9A
if($Action -ne 'inspect'){
    if($dialogs.Count -ne 1){throw 'Expected exactly one native owned dialog'}
    $buttonCondition=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Button)
    $buttons=$dialogs[0].element.FindAll([System.Windows.Automation.TreeScope]::Descendants,$buttonCondition)
    $matchedButtons=@($buttons|Where-Object {if($Action -eq 'acknowledge'){$_.Current.Name -eq $ackLabel}else{$_.Current.Name -in @('OK',$okLabel)}})
    if($matchedButtons.Count -eq 1){
        $invoke=$matchedButtons[0].GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $invoke.Invoke();$report.method='UIAutomation.Invoke'
    }else{
        $nativeButtons=@($dialogs[0].native_children|Where-Object{$_.class_name -eq 'Button' -and $_.enabled -and $_.visible -and $(if($Action -eq 'acknowledge'){$_.text -eq $ackLabel}else{$_.text -in @('OK',$okLabel)})})
        if($nativeButtons.Count -ne 1){throw 'Owned native button is ambiguous; exact child class and caption required'}
        [OwnedDialogNative]::Click($dialogs[0].hwnd,$nativeButtons[0].hwnd);$report.method='owned Win32 Button BM_CLICK'
    }
    $report.invoked=$true
}
if(Test-Path $OutputFile){throw 'Evidence file already exists'}
[IO.File]::WriteAllText($OutputFile,($report|ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
