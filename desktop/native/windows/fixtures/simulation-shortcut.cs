using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Threading;
using System.Web.Script.Serialization;

public static class SimulationShortcut {
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
    static Process Verify(Dictionary<string, object> facts) {
        var process = Process.GetProcessById(Convert.ToInt32(facts["pid"], CultureInfo.InvariantCulture));
        Require(!process.HasExited && process.StartTime.ToUniversalTime().Ticks.ToString(CultureInfo.InvariantCulture) == (string)facts["birth_ticks"], "Owned process lifetime differs");
        string image = Path.GetFullPath(process.MainModule.FileName);
        Require(String.Equals(image, Path.GetFullPath((string)facts["image"]), StringComparison.OrdinalIgnoreCase), "Owned process image differs");
        Require(Hash(image) == (string)facts["sha256"], "Owned process image digest differs");
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
            Verify(target);
            Require(Integrity(Process.GetCurrentProcess()) == 8192, "Injector must run at Medium");
            foreach (int key in new int[] { 0x10, 0x11, 0x12, 0x5b, 0x5c, 0xa0, 0xa1, 0xa2, 0xa3, 0xa4, 0xa5 })
                Require((GetAsyncKeyState(key) & 0x8000) == 0, "A modifier was already physically held");
            string action = args[9], role = (string)target["role"];
            int count = 0;
            if (action == "toggle") {
                Require(role == "notepad" || role == "chrome" || role == "main", "Toggle target role denied");
                count = Send(pid, window, new ushort[] { 0x11, 0x10, 0x20 });
            } else if (action == "escape") {
                Require(role == "main", "Escape is application-local only");
                count = Send(pid, window, new ushort[] { 0x1b });
            } else if (action == "menu-quit") {
                Require(role == "main" || role == "normal-main", "Quit is restricted to an owned product window");
                foreach (ushort key in new ushort[] { 0x79, 0x24, 0x28, 0x24, 0x0d }) {
                    count += Send(pid, window, new ushort[] { key });
                    Thread.Sleep(90);
                }
            } else throw new InvalidOperationException("Action is outside the fixed test allowlist");
            Console.WriteLine(serializer.Serialize(new { sent = true, action = action, event_count = count, integrity_rid = 8192, owned_foreground_verified = true }));
            return 0;
        } catch (Exception error) {
            Console.Error.WriteLine("Independent fixed shortcut injector failed. " + error.Message);
            return 2;
        }
    }
}
