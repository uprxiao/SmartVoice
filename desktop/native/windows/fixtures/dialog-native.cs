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
