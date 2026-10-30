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
$hasher=[Security.Cryptography.SHA256]::Create()
try{$actualHash=[BitConverter]::ToString($hasher.ComputeHash([IO.File]::ReadAllBytes($process.Path))).Replace('-','')}finally{$hasher.Dispose()}
if($facts.ParentProcessId -ne $OwnerPid -or $actualHash -ne $ExpectedExeSha256){throw 'Refusing a dialog outside the exact owned Electron process'}
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @'
using System;using System.Collections.Generic;using System.Runtime.InteropServices;using System.Text;
public sealed class OwnedDialogButton { public long hwnd;public string text;public string class_name;public bool enabled;public bool visible; }
public static class OwnedDialogNative {
 delegate bool Callback(IntPtr window,IntPtr data);
 [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr window,Callback callback,IntPtr data);
 [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window,out uint pid);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr window,StringBuilder text,int count);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr window,StringBuilder text,int count);
 [DllImport("user32.dll")] static extern bool IsWindowEnabled(IntPtr window);
 [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr window);
 [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
 [DllImport("user32.dll",SetLastError=true)] static extern IntPtr SendMessageTimeout(IntPtr window,uint msg,IntPtr wp,IntPtr lp,uint flags,uint timeout,out UIntPtr result);
 public static OwnedDialogButton[] Children(long dialog,uint expectedPid){
  var rows=new List<OwnedDialogButton>();EnumChildWindows(new IntPtr(dialog),(window,data)=>{
   uint pid;GetWindowThreadProcessId(window,out pid);if(pid!=expectedPid)throw new Exception("Unexpected child process");
   var cls=new StringBuilder(256);var text=new StringBuilder(2048);GetClassName(window,cls,cls.Capacity);GetWindowText(window,text,text.Capacity);
   rows.Add(new OwnedDialogButton{hwnd=window.ToInt64(),text=text.ToString(),class_name=cls.ToString(),enabled=IsWindowEnabled(window),visible=IsWindowVisible(window)});return true;
  },IntPtr.Zero);return rows.ToArray();
 }
 public static void Click(long dialog,long button){SetForegroundWindow(new IntPtr(dialog));UIntPtr result;if(SendMessageTimeout(new IntPtr(button),0xF5,IntPtr.Zero,IntPtr.Zero,2,5000,out result)==IntPtr.Zero)throw new Exception("Owned native button action timeout");}
}
'@
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
