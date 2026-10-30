// Scoped fault preparation for an otherwise idle disposable CI desktop only.
// Original desktop/process security is retained in memory and restored before
// handles are closed. No settings on the original desktop are changed.
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Threading;

public static class RemainingBoundaryControl {
    [DllImport("user32.dll",SetLastError=true)] static extern IntPtr OpenInputDesktop(uint flags,bool inherit,uint access);
    [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateDesktop(string name,IntPtr device,IntPtr mode,uint flags,uint access,IntPtr attributes);
    [DllImport("user32.dll",SetLastError=true)] static extern bool SwitchDesktop(IntPtr desktop);
    [DllImport("user32.dll",SetLastError=true)] static extern bool SetThreadDesktop(IntPtr desktop);
    [DllImport("user32.dll")] static extern IntPtr GetThreadDesktop(uint thread);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
    [DllImport("user32.dll",SetLastError=true)] static extern bool CloseDesktop(IntPtr desktop);
    [DllImport("user32.dll",SetLastError=true)] static extern bool SetUserObjectSecurity(IntPtr obj,ref uint information,IntPtr descriptor);
    [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool ConvertStringSecurityDescriptorToSecurityDescriptor(string sddl,uint revision,out IntPtr descriptor,out uint length);
    [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr block);
    [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr OpenProcess(uint access,bool inherit,uint pid);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("advapi32.dll",SetLastError=true)] static extern bool GetKernelObjectSecurity(IntPtr handle,uint information,byte[] descriptor,uint length,out uint needed);
    [DllImport("advapi32.dll",SetLastError=true)] static extern bool SetKernelObjectSecurity(IntPtr handle,uint information,byte[] descriptor);
    static readonly ManualResetEvent ready=new ManualResetEvent(false),release=new ManualResetEvent(false),done=new ManualResetEvent(false);
    static Thread desktopThread;
    static Exception desktopError;
    static bool desktopRestored;
    static IntPtr processHandle;
    static byte[] originalProcessDescriptor;
    static void Guard(){if(Environment.GetEnvironmentVariable("GITHUB_ACTIONS")!="true")throw new Exception("Disposable CI boundary fixture required");}
    static void Check(bool value,string operation){if(!value)throw new Win32Exception(Marshal.GetLastWin32Error(),operation);}
    static string Sid(){using(var identity=WindowsIdentity.GetCurrent()){return identity.User.Value;}}
    static byte[] Descriptor(string value){IntPtr pointer;uint length;Check(ConvertStringSecurityDescriptorToSecurityDescriptor(value,1,out pointer,out length),"Parse scoped descriptor");try{var bytes=new byte[length];Marshal.Copy(pointer,bytes,0,(int)length);return bytes;}finally{LocalFree(pointer);}}

    public static void EnterEmptyDesktop(bool denyNewOpens){
        Guard();if(desktopThread!=null)throw new Exception("Desktop control already active");
        ready.Reset();release.Reset();done.Reset();desktopError=null;desktopRestored=false;
        desktopThread=new Thread(delegate(){
            IntPtr original=IntPtr.Zero,created=IntPtr.Zero;
            IntPtr originalThread=GetThreadDesktop(GetCurrentThreadId());
            try{
                original=OpenInputDesktop(0,false,0x100);Check(original!=IntPtr.Zero,"Retain original input desktop");
                created=CreateDesktop("SVI-owned-"+Guid.NewGuid().ToString("N"),IntPtr.Zero,IntPtr.Zero,0,0x000F01FF,IntPtr.Zero);Check(created!=IntPtr.Zero,"Create owned empty desktop");
                if(denyNewOpens){
                    IntPtr descriptor;uint length;Check(ConvertStringSecurityDescriptorToSecurityDescriptor("D:(D;;GA;;;"+Sid()+")",1,out descriptor,out length),"Parse owned desktop DACL");
                    try{uint dacl=4;Check(SetUserObjectSecurity(created,ref dacl,descriptor),"Set only owned desktop DACL");}finally{LocalFree(descriptor);}
                }
                Check(SwitchDesktop(created),"Switch to owned empty desktop");ready.Set();
                release.WaitOne(20000); // Independent recovery even if the controller fails.
            }catch(Exception error){desktopError=error;ready.Set();}
            finally{
                try{if(original!=IntPtr.Zero){Check(SwitchDesktop(original),"Restore original input desktop");Check(SetThreadDesktop(originalThread),"Restore control thread desktop");desktopRestored=true;}}
                catch(Exception error){desktopError=error;}
                if(created!=IntPtr.Zero&&!CloseDesktop(created))desktopError=new Win32Exception(Marshal.GetLastWin32Error(),"Release only owned desktop");
                if(original!=IntPtr.Zero)CloseDesktop(original);
                done.Set();
            }
        });
        desktopThread.IsBackground=true;desktopThread.Start();
        if(!ready.WaitOne(5000)){release.Set();throw new Exception("Owned desktop preparation deadline");}
        if(desktopError!=null){release.Set();throw desktopError;}
    }
    public static bool RestoreDesktop(){
        if(desktopThread==null)return true;release.Set();
        if(!done.WaitOne(5000))throw new Exception("Original desktop restoration not confirmed");
        desktopThread.Join();desktopThread=null;
        if(desktopError!=null)throw desktopError;
        return desktopRestored;
    }
    public static void DenyOwnedProcessQuery(uint pid){
        Guard();if(processHandle!=IntPtr.Zero)throw new Exception("Process security control already active");
        processHandle=OpenProcess(0x00060000,false,pid);Check(processHandle!=IntPtr.Zero,"Retain owned process READ_CONTROL/WRITE_DAC");
        uint size;GetKernelObjectSecurity(processHandle,4,null,0,out size);if(size==0)throw new Exception("Original descriptor unavailable");
        originalProcessDescriptor=new byte[size];Check(GetKernelObjectSecurity(processHandle,4,originalProcessDescriptor,size,out size),"Read original owned process DACL");
        var descriptor=Descriptor("D:(D;;0x1400;;;"+Sid()+")(A;;GA;;;"+Sid()+")");
        Check(SetKernelObjectSecurity(processHandle,4,descriptor),"Deny only fresh query handles to owned process");
    }
    public static bool RestoreProcessSecurity(){
        if(processHandle==IntPtr.Zero)return true;
        try{
            if(originalProcessDescriptor!=null)Check(SetKernelObjectSecurity(processHandle,4,originalProcessDescriptor),"Restore exact original owned process DACL");
            return true;
        }
        finally{CloseHandle(processHandle);processHandle=IntPtr.Zero;originalProcessDescriptor=null;}
    }
}
