using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation;

public static class SimulationShortcut {
    static Dictionary<string, object> menuDiagnostic;
    sealed class MenuInvocation {
        public readonly ManualResetEvent done = new ManualResetEvent(false);
        public volatile string kind = "pending";
        public Exception error;
    }
    [StructLayout(LayoutKind.Sequential)] struct INPUT { public uint type; public INPUTUNION data; }
    [StructLayout(LayoutKind.Explicit)] struct INPUTUNION { [FieldOffset(0)] public KEYBDINPUT key; [FieldOffset(0)] public MOUSEINPUT mouse; }
    [StructLayout(LayoutKind.Sequential)] struct KEYBDINPUT { public ushort vk, scan; public uint flags, time; public UIntPtr extra; }
    [StructLayout(LayoutKind.Sequential)] struct MOUSEINPUT { public int x, y; public uint data, flags, time; public UIntPtr extra; }
    [DllImport("user32.dll")] static extern uint SendInput(uint count, INPUT[] input, int size);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern IntPtr GetAncestor(IntPtr window, uint flags);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("user32.dll")] static extern short GetAsyncKeyState(int key);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr data, int size, out int needed);
    [DllImport("advapi32.dll")] static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);
    [DllImport("advapi32.dll")] static extern IntPtr GetSidSubAuthority(IntPtr sid, uint index);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    static extern bool QueryFullProcessImageNameW(IntPtr process, uint flags, StringBuilder image, ref uint size);

    static void Require(bool value, string message) { if (!value) throw new InvalidOperationException(message); }
    static int Integrity(Process process) {
        IntPtr token;
        Require(OpenProcessToken(process.Handle, 8, out token), "Process token unavailable");
        try {
            int needed;
            GetTokenInformation(token, 25, IntPtr.Zero, 0, out needed);
            Require(needed > 0 && needed < 4096, "Invalid token information length");
            IntPtr data = Marshal.AllocHGlobal(needed);
            try {
                Require(GetTokenInformation(token, 25, data, needed, out needed), "Integrity information unavailable");
                IntPtr sid = Marshal.ReadIntPtr(data);
                byte count = Marshal.ReadByte(GetSidSubAuthorityCount(sid));
                Require(count > 0, "Integrity SID unavailable");
                return Marshal.ReadInt32(GetSidSubAuthority(sid, (uint)(count - 1)));
            } finally { Marshal.FreeHGlobal(data); }
        } finally { CloseHandle(token); }
    }
    static string Hash(string file) {
        using (var input = File.OpenRead(file)) using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(input)).Replace("-", "").ToLowerInvariant();
    }
    static string ProcessImage(IntPtr processHandle) {
        uint length = 32768;
        var image = new StringBuilder((int)length);
        if (!QueryFullProcessImageNameW(processHandle, 0, image, ref length)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        Require(length > 0 && length < image.Capacity, "Process image query returned invalid length");
        return Path.GetFullPath(image.ToString(0, (int)length));
    }
    static Process Verify(Dictionary<string, object> facts) {
        var process = Process.GetProcessById(Convert.ToInt32(facts["pid"], CultureInfo.InvariantCulture));
        IntPtr processHandle = process.Handle;
        Require(!process.HasExited && process.StartTime.ToUniversalTime().Ticks.ToString(CultureInfo.InvariantCulture) == (string)facts["birth_ticks"], "Owned process lifetime differs");
        string image = ProcessImage(processHandle);
        Require(String.Equals(image, Path.GetFullPath((string)facts["image"]), StringComparison.OrdinalIgnoreCase), "Owned process image differs");
        Require(Hash(image) == (string)facts["sha256"], "Owned process image digest differs");
        process.Refresh();
        Require(!process.HasExited && process.StartTime.ToUniversalTime().Ticks.ToString(CultureInfo.InvariantCulture) == (string)facts["birth_ticks"], "Owned process lifetime changed during image verification");
        Require(Integrity(process) == 8192, "Owned target must be Medium");
        return process;
    }
    static void Foreground(int pid, IntPtr window) {
        uint actual;
        IntPtr current = GetForegroundWindow();
        GetWindowThreadProcessId(current, out actual);
        Require(actual == pid && current == window && GetAncestor(current, 2) == window, "Explicit owned root is no longer foreground");
    }
    static INPUT Key(ushort key, bool up) {
        return new INPUT { type = 1, data = new INPUTUNION { key = new KEYBDINPUT { vk = key, flags = up ? 2u : 0u } } };
    }
    static int Send(int pid, IntPtr window, ushort[] keys) {
        Foreground(pid, window);
        var input = new INPUT[keys.Length * 2];
        for (int i = 0; i < keys.Length; i++) input[i] = Key(keys[i], false);
        for (int i = 0; i < keys.Length; i++) input[keys.Length + i] = Key(keys[keys.Length - i - 1], true);
        Require(SendInput((uint)input.Length, input, Marshal.SizeOf(typeof(INPUT))) == input.Length, "OS did not accept the complete fixed chord");
        return input.Length;
    }
    static string MenuName(string name) { return (name ?? "").Split('\t')[0].Replace("&", "").Trim(); }
    static List<AutomationElement> VisibleMenus(AutomationElement root, int pid, ControlType type) {
        var result = new List<AutomationElement>();
        var elements = root.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, type));
        foreach (AutomationElement element in elements) {
            try { if (element.Current.ProcessId == pid && element.Current.IsEnabled && !element.Current.IsOffscreen) result.Add(element); }
            catch (ElementNotAvailableException) { }
        }
        return result;
    }
    static MenuInvocation MenuAction(AutomationElement item, bool expand) {
        var invocation = new MenuInvocation();
        var thread = new Thread(() => {
            try {
                object pattern;
                if (expand && item.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out pattern)) {
                    invocation.kind = "expand-collapse";
                    var expansion = (ExpandCollapsePattern)pattern;
                    if (expansion.Current.ExpandCollapseState != ExpandCollapseState.Expanded) expansion.Expand();
                } else if (item.TryGetCurrentPattern(InvokePattern.Pattern, out pattern)) {
                    invocation.kind = "invoke";
                    ((InvokePattern)pattern).Invoke();
                } else throw new InvalidOperationException("Verified menu item has no supported UI action");
            } catch (Exception error) { invocation.error = error; }
            finally { invocation.done.Set(); }
        });
        thread.IsBackground = true;
        thread.SetApartmentState(ApartmentState.MTA);
        thread.Start();
        return invocation;
    }
    static Dictionary<string, object> QuitMenu(Process process, IntPtr window, string role) {
        int pid = process.Id;
        string product = role == "normal-main" ? "SmartVoiceInput" : "SmartVoiceInput Simulation";
        menuDiagnostic = new Dictionary<string, object> { { "symbol", "OS_MENU_QUIT" }, { "stage", "find-product-menu" }, { "product_menu_name", product }, { "target_pid", pid } };
        Foreground(pid, window);
        AutomationElement root = AutomationElement.FromHandle(window);
        Require(root.Current.ProcessId == pid, "Actual OS menu root is not the verified product process");
        AutomationElement productMenu = null;
        var clock = Stopwatch.StartNew();
        do {
            var names = new List<string>();
            int matches = 0;
            var seen = new HashSet<string>();
            foreach (AutomationElement bar in VisibleMenus(root, pid, ControlType.MenuBar)) {
                var entries = VisibleMenus(bar, pid, ControlType.Button);
                entries.AddRange(VisibleMenus(bar, pid, ControlType.MenuItem));
                foreach (AutomationElement item in entries) {
                    string identity = String.Join(",", Array.ConvertAll(item.GetRuntimeId(), value => value.ToString(CultureInfo.InvariantCulture)));
                    if (!seen.Add(identity)) continue;
                    string name = MenuName(item.Current.Name);
                    if (names.Count < 16 && name.Length <= 96) names.Add(name);
                    object pattern;
                    bool actionable = item.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out pattern) || item.TryGetCurrentPattern(InvokePattern.Pattern, out pattern);
                    if (name == product && actionable) { productMenu = item; matches++; }
                }
            }
            menuDiagnostic["observed_menu_names"] = names;
            menuDiagnostic["product_menu_matches"] = matches;
            Require(matches <= 1, "Ambiguous actual product menu");
            if (matches == 1) break;
            Require(clock.ElapsedMilliseconds < 5000 && !process.HasExited, "Actual product menu was not exposed within its observation deadline");
            process.WaitForExit(40);
        } while (true);
        menuDiagnostic["stage"] = "expand-product-menu";
        menuDiagnostic["product_menu_pid"] = productMenu.Current.ProcessId;
        menuDiagnostic["product_menu_control_type"] = productMenu.Current.ControlType.ProgrammaticName;
        Foreground(pid, window);
        var opening = MenuAction(productMenu, true);
        menuDiagnostic["stage"] = "find-exit-menu";
        AutomationElement quit = null;
        clock.Restart();
        do {
            var seen = new HashSet<string>();
            var candidates = new List<AutomationElement>();
            var roots = AutomationElement.RootElement.FindAll(TreeScope.Children, new PropertyCondition(AutomationElement.ProcessIdProperty, pid));
            foreach (AutomationElement surface in roots) foreach (AutomationElement item in VisibleMenus(surface, pid, ControlType.MenuItem)) {
                string name = MenuName(item.Current.Name);
                if (name != "Exit") continue;
                string identity = String.Join(",", Array.ConvertAll(item.GetRuntimeId(), value => value.ToString(CultureInfo.InvariantCulture)));
                if (seen.Add(identity)) candidates.Add(item);
            }
            menuDiagnostic["quit_menu_matches"] = candidates.Count;
            Require(candidates.Count <= 1, "Ambiguous visible product Quit menu item");
            if (candidates.Count == 1) { quit = candidates[0]; break; }
            Require(!opening.done.WaitOne(0) || opening.error == null, "Product menu UI action failed before its Exit item appeared");
            Require(clock.ElapsedMilliseconds < 5000 && !process.HasExited, "Expanded product menu did not expose its Quit item");
            process.WaitForExit(40);
        } while (true);
        menuDiagnostic["stage"] = "invoke-quit";
        menuDiagnostic["quit_menu_name"] = MenuName(quit.Current.Name);
        menuDiagnostic["quit_menu_pid"] = quit.Current.ProcessId;
        IntPtr menuForeground = GetForegroundWindow();
        uint menuForegroundPid;
        GetWindowThreadProcessId(menuForeground, out menuForegroundPid);
        menuDiagnostic["menu_foreground_pid"] = menuForegroundPid;
        Require(menuForegroundPid == pid && (menuForeground == window || GetAncestor(menuForeground, 3) == window), "Product menu foreground left the verified Main root/owner chain");
        var exiting = MenuAction(quit, false);
        menuDiagnostic["stage"] = "wait-process-exit";
        Require(process.WaitForExit(8000) && process.ExitCode == 0, "Actual OS product Quit action did not terminate its verified process cleanly");
        menuDiagnostic["open_action"] = opening.kind;
        menuDiagnostic["quit_action"] = exiting.kind;
        menuDiagnostic["open_action_returned"] = opening.done.WaitOne(0);
        menuDiagnostic["quit_action_returned"] = exiting.done.WaitOne(0);
        if (opening.done.WaitOne(0) && opening.error != null) menuDiagnostic["open_action_error"] = opening.error.GetType().Name;
        if (exiting.done.WaitOne(0) && exiting.error != null) menuDiagnostic["quit_action_error"] = exiting.error.GetType().Name;
        menuDiagnostic["stage"] = "exit-confirmed";
        menuDiagnostic["menu_owner_verified"] = true;
        return menuDiagnostic;
    }
    public static int Main(string[] args) {
        try {
            Require(Environment.GetEnvironmentVariable("GITHUB_ACTIONS") == "true", "Disposable CI only");
            Require(args.Length == 10 && args[0] == "--manifest" && args[2] == "--nonce" && args[4] == "--pid" && args[6] == "--hwnd" && args[8] == "--action", "Fixed injector arguments required");
            string file = Path.GetFullPath(args[1]);
            string temp = Path.GetFullPath(Environment.GetEnvironmentVariable("RUNNER_TEMP") ?? "").TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            Require(file.StartsWith(temp, StringComparison.OrdinalIgnoreCase) && new FileInfo(file).Length <= 65536, "Manifest must belong to this runner temporary directory");
            var serializer = new JavaScriptSerializer();
            var manifest = serializer.Deserialize<Dictionary<string, object>>(File.ReadAllText(file));
            Require((string)manifest["schema"] == "svi-simulation-smoke-owner-v1" && (string)manifest["nonce"] == args[3], "Owned manifest identity differs");
            Verify((Dictionary<string, object>)manifest["owner"]);
            int pid = Int32.Parse(args[5], CultureInfo.InvariantCulture);
            IntPtr window = new IntPtr(Int64.Parse(args[7], CultureInfo.InvariantCulture));
            Dictionary<string, object> target = null;
            foreach (var item in (IEnumerable)manifest["targets"]) {
                var row = (Dictionary<string, object>)item;
                if (Convert.ToInt32(row["pid"], CultureInfo.InvariantCulture) == pid) { Require(target == null, "Ambiguous owned target"); target = row; }
            }
            Require(target != null && pid > 0 && window != IntPtr.Zero, "Explicit owned target required");
            Process targetProcess = Verify(target);
            Require(Integrity(Process.GetCurrentProcess()) == 8192, "Injector must run at Medium");
            foreach (int key in new int[] { 0x10, 0x11, 0x12, 0x5b, 0x5c, 0xa0, 0xa1, 0xa2, 0xa3, 0xa4, 0xa5 })
                Require((GetAsyncKeyState(key) & 0x8000) == 0, "A modifier was already physically held");
            string action = args[9], role = (string)target["role"];
            int count = 0;
            Dictionary<string, object> menu = null;
            if (action == "toggle") {
                Require(role == "notepad" || role == "chrome" || role == "main", "Toggle target role denied");
                count = Send(pid, window, new ushort[] { 0x11, 0x10, 0x20 });
            } else if (action == "escape") {
                Require(role == "main", "Escape is application-local only");
                count = Send(pid, window, new ushort[] { 0x1b });
            } else if (action == "menu-quit") {
                Require(role == "main" || role == "normal-main", "Quit is restricted to an owned product window");
                menu = QuitMenu(targetProcess, window, role);
            } else throw new InvalidOperationException("Action is outside the fixed test allowlist");
            Console.WriteLine(serializer.Serialize(new { sent = true, action = action, event_count = count, integrity_rid = 8192, owned_foreground_verified = true, process_exit_confirmed = menu != null, menu = menu }));
            return 0;
        } catch (Exception error) {
            Console.Error.WriteLine("Independent fixed shortcut injector failed. " + error.Message);
            if (menuDiagnostic != null) Console.Error.WriteLine(new JavaScriptSerializer().Serialize(menuDiagnostic));
            return 2;
        }
    }
}
