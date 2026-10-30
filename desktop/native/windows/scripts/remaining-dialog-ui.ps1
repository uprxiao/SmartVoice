param(
    [Parameter(Mandatory=$true)][int]$TargetPid,
    [Parameter(Mandatory=$true)][int]$OwnerPid,
    [Parameter(Mandatory=$true)][string]$ExpectedExeSha256,
    [Parameter(Mandatory=$true)][string]$OutputFile,
    [ValidateSet('inspect','acknowledge','error-acknowledge')][string]$Action='inspect'
)
# Run with Windows PowerShell 5.1 for the installed .NET UIAutomation assemblies.
$ErrorActionPreference='Stop'
if($env:GITHUB_ACTIONS -ne 'true' -or $ExpectedExeSha256 -notmatch '^[a-fA-F0-9]{64}$'){throw 'Disposable CI UI observer required'}
$process=Get-Process -Id $TargetPid -ErrorAction Stop
$facts=Get-CimInstance Win32_Process -Filter "ProcessId=$TargetPid"
if($facts.ParentProcessId -ne $OwnerPid -or (Get-FileHash $process.Path -Algorithm SHA256).Hash -ne $ExpectedExeSha256){throw 'Refusing a dialog outside the exact owned Electron process'}
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root=[System.Windows.Automation.AutomationElement]::RootElement
$pidCondition=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty,$TargetPid)
$elements=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,$pidCondition)
$dialogs=@()
foreach($element in $elements){
    if($element.Current.ClassName -eq '#32770'){
        $condition=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Button)
        $buttons=$element.FindAll([System.Windows.Automation.TreeScope]::Descendants,$condition)
        $dialogs+=@{element=$element;hwnd=$element.Current.NativeWindowHandle;buttons=@($buttons|ForEach-Object{$_.Current.Name})}
    }
}
$report=@{action=$Action;dialogs=@($dialogs|ForEach-Object{@{hwnd=$_.hwnd;buttons=$_.buttons}});invoked=$false}
$ackLabel=([string][char]0x77E5)+[char]0x9053+[char]0x4E86
$okLabel=([string][char]0x786E)+[char]0x5B9A
if($Action -ne 'inspect'){
    if($dialogs.Count -ne 1){throw 'Expected exactly one native owned dialog'}
    $buttonCondition=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Button)
    $buttons=$dialogs[0].element.FindAll([System.Windows.Automation.TreeScope]::Descendants,$buttonCondition)
    $matchedButtons=@($buttons|Where-Object {if($Action -eq 'acknowledge'){$_.Current.Name -eq $ackLabel}else{$_.Current.Name -in @('OK',$okLabel)}})
    if($matchedButtons.Count -ne 1){throw 'Native dialog button is ambiguous or differs from the frozen expectation'}
    $invoke=$matchedButtons[0].GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $invoke.Invoke();$report.invoked=$true
}
if(Test-Path $OutputFile){throw 'Evidence file already exists'}
[IO.File]::WriteAllText($OutputFile,($report|ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
