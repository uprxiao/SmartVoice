// Owned classic Win32 Edit: acquire actual HWND reuse, not a fake numeric handle.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

public sealed class HandleReuseFixture : Form {
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern IntPtr CreateWindowEx(uint ex,string cls,string text,uint style,int x,int y,int w,int h,IntPtr parent,IntPtr menu,IntPtr instance,IntPtr arg);
    [DllImport("user32.dll")] static extern bool DestroyWindow(IntPtr window);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr window);
    [DllImport("imm32.dll")] static extern bool ImmDisableIME(uint thread);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr window,StringBuilder name,int count);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window,out uint pid);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern IntPtr GetProp(IntPtr window,string name);
    [DllImport("user32.dll")] static extern IntPtr SetFocus(IntPtr window);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll",EntryPoint="SetWindowLongPtrW")] static extern IntPtr SetWindowLongPtr(IntPtr window,int index,IntPtr value);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern IntPtr CallWindowProc(IntPtr proc,IntPtr window,uint msg,IntPtr wp,IntPtr lp);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern IntPtr SendMessage(IntPtr window,uint msg,IntPtr wp,IntPtr lp);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr window,StringBuilder text,int max);
    [DllImport("user32.dll")] static extern int GetWindowTextLength(IntPtr window);
    delegate IntPtr WindowProcedure(IntPtr window,uint msg,IntPtr wp,IntPtr lp);
    readonly string folder;
    readonly JavaScriptSerializer json=new JavaScriptSerializer();
    readonly HashSet<string> seen=new HashSet<string>();
    readonly WindowProcedure hook;
    IntPtr edit,originalProcedure,firstHandle;
    int pastes,enters;
    bool working;
    readonly Timer timer=new Timer();

    HandleReuseFixture(string directory) {
        folder=Path.GetFullPath(directory);Text="SVI actual HWND reuse fixture";Width=760;Height=300;
        hook=Procedure;
        Shown+=delegate {
            edit=NewEdit();firstHandle=edit;Install();FocusEdit();
            Write("ready.json",State());timer.Interval=30;timer.Tick+=Tick;timer.Start();
        };
    }
    IntPtr NewEdit(){
        var window=CreateWindowEx(0,"Edit","BEGIN React END",0x50010080,24,50,650,36,Handle,new IntPtr(100),IntPtr.Zero,IntPtr.Zero);
        if(window==IntPtr.Zero)throw new Exception("Owned Edit creation failed");
        SendMessage(window,0xB1,new IntPtr(6),new IntPtr(11));return window;
    }
    void Install(){originalProcedure=SetWindowLongPtr(edit,-4,Marshal.GetFunctionPointerForDelegate(hook));if(originalProcedure==IntPtr.Zero)throw new Exception("Owned input observer unavailable");}
    IntPtr Procedure(IntPtr window,uint msg,IntPtr wp,IntPtr lp){
        if(window==edit){if(msg==0x302)pastes++;if(msg==0x100&&wp.ToInt64()==13)enters++;}
        return CallWindowProc(originalProcedure,window,msg,wp,lp);
    }
    void FocusEdit(){SetForegroundWindow(Handle);SetFocus(edit);}
    object State(){
        var text=new StringBuilder(GetWindowTextLength(edit)+1);GetWindowText(edit,text,text.Capacity);
        uint selection=unchecked((uint)SendMessage(edit,0xB0,IntPtr.Zero,IntPtr.Zero).ToInt64());
        return new {pid=Process.GetCurrentProcess().Id,root_hwnd=Handle.ToInt64(),edit_hwnd=edit.ToInt64(),original_edit_hwnd=firstHandle.ToInt64(),text=text.ToString(),start=selection&65535,end=selection>>16,paste_count=pastes,enter_count=enters};
    }
    object Reuse(){
        var beforeProperty=GetProp(edit,"SmartVoiceInput.TargetLifecycle.v1").ToInt64();
        var watch=Stopwatch.StartNew();if(!DestroyWindow(edit))throw new Exception("Original HWND was not destroyed");
        bool destroyed=!IsWindow(firstHandle);long propertyAfterDestroy=GetProp(firstHandle,"SmartVoiceInput.TargetLifecycle.v1").ToInt64();
        var slotSamples=new List<object>();
        int attempts=0;bool acquired=false;
        edit=IntPtr.Zero;
        while(attempts<100000&&watch.ElapsedMilliseconds<60000){
            if(edit!=IntPtr.Zero&&!DestroyWindow(edit))throw new Exception("Intermediate owned HWND was not destroyed");
            attempts++;edit=NewEdit();
            if(attempts==1)SetFocus(edit);
            if(attempts<=4||attempts%10000==0){uint owner;GetWindowThreadProcessId(firstHandle,out owner);var cls=new StringBuilder(256);GetClassName(firstHandle,cls,cls.Capacity);slotSamples.Add(new{attempt=attempts,candidate=edit.ToInt64(),original_slot_valid=IsWindow(firstHandle),original_slot_pid=owner,original_slot_class=cls.ToString()});}
            if(edit==firstHandle){acquired=true;break;}
            if(attempts%1000==0)Application.DoEvents();
        }
        if(edit==IntPtr.Zero)edit=NewEdit();
        pastes=0;enters=0;Install();FocusEdit();
        return new {actual_same_hwnd_acquired=acquired,attempts=attempts,elapsed_ms=watch.ElapsedMilliseconds,original_destroyed=destroyed,property_before_destroy=beforeProperty,property_after_destroy=propertyAfterDestroy,slot_samples=slotSamples,state=State()};
    }
    void Write(string name,object value){var file=Path.Combine(folder,name);if(File.Exists(file))throw new Exception("Evidence already exists");File.WriteAllText(file+".tmp",json.Serialize(value),new UTF8Encoding(false));File.Move(file+".tmp",file);}
    void Tick(object sender,EventArgs e){
        if(working)return;working=true;
        try{foreach(var file in Directory.GetFiles(folder,"*.request.json").OrderBy(f=>int.Parse(Path.GetFileName(f).Split('.')[0]))){
            if(!seen.Add(file))continue;var request=json.Deserialize<Dictionary<string,object>>(File.ReadAllText(file,Encoding.UTF8));object value;
            try{switch((string)request["action"]){
                case "state":value=State();break;
                case "focus":FocusEdit();value=State();break;
                case "reuse":value=Reuse();break;
                case "reset":if(IsWindow(edit))DestroyWindow(edit);edit=NewEdit();pastes=0;enters=0;Install();FocusEdit();value=State();break;
                case "quit":value=new {closing=true};BeginInvoke(new Action(Close));break;
                default:throw new Exception("Unknown owned HWND fixture action");
            }}catch(Exception error){value=new {error=error.Message};}
            Write(Path.GetFileName(file).Replace(".request.",".response."),value);
        }}finally{working=false;}
    }
    [STAThread] public static void Main(string[] args){
        if(Environment.GetEnvironmentVariable("GITHUB_ACTIONS")!="true"||args.Length!=1)throw new Exception("Disposable Windows fixture only");
        string folder=Path.GetFullPath(args[0]);string root=Path.GetFullPath(Environment.GetEnvironmentVariable("SVI_REMAINING_ROOT"));
        if(!folder.StartsWith(root+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase)||!Directory.Exists(folder))throw new Exception("Owned directory required");
        // Only this fictional process: avoid allocating a new IME helper window
        // into the just-retired Edit slot. This case does not test physical IME.
        ImmDisableIME(0xffffffff);
        Application.EnableVisualStyles();Application.Run(new HandleReuseFixture(folder));
    }
}
