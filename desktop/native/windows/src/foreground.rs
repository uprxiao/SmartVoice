//! Foreground window facts and admission checks. The product never steals the foreground:
//! the paste target is simply whatever window the user is in at paste time.
use crate::{integrity, R};
use serde::Serialize;
use windows::core::BOOL;
use windows::Win32::Foundation::{HWND, LPARAM, RECT};
use windows::Win32::System::StationsAndDesktops::{
    CloseDesktop, OpenInputDesktop, DESKTOP_CONTROL_FLAGS, DESKTOP_SWITCHDESKTOP,
};
use windows::Win32::System::Threading::GetCurrentProcessId;
use windows::Win32::UI::Input::KeyboardAndMouse::{VIRTUAL_KEY, VK_F24};
use windows::Win32::UI::WindowsAndMessaging::{
    EnumWindows, GetAncestor, GetClassNameW, GetForegroundWindow, GetWindow, GetWindowRect,
    GetWindowTextW, GetWindowThreadProcessId, IsWindowVisible, SetForegroundWindow, GA_ROOT,
    GW_OWNER,
};

#[derive(Debug, Clone, Serialize)]
pub struct ForegroundInfo {
    pub hwnd: isize,
    pub pid: u32,
    pub process_birth_100ns: Option<u64>,
    pub tid: u32,
    pub class: String,
    pub title: String,
    pub visible: bool,
    pub rect: [i32; 4],
    pub is_own_process: bool,
    pub is_shell: bool,
}

const SHELL_CLASSES: &[&str] = &[
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland",
];

pub fn class_of(hwnd: HWND) -> String {
    let mut buf = [0u16; 256];
    let n = unsafe { GetClassNameW(hwnd, &mut buf) };
    String::from_utf16_lossy(&buf[..n.max(0) as usize])
}

pub fn title_of(hwnd: HWND) -> String {
    let mut buf = [0u16; 512];
    let n = unsafe { GetWindowTextW(hwnd, &mut buf) };
    String::from_utf16_lossy(&buf[..n.max(0) as usize])
}

pub fn describe(hwnd: HWND) -> ForegroundInfo {
    let mut pid = 0u32;
    let tid = unsafe { GetWindowThreadProcessId(hwnd, Some(&mut pid)) };
    let mut r = RECT::default();
    let _ = unsafe { GetWindowRect(hwnd, &mut r) };
    let class = class_of(hwnd);
    ForegroundInfo {
        hwnd: hwnd.0 as isize,
        pid,
        process_birth_100ns: integrity::process_birth(pid),
        tid,
        is_shell: SHELL_CLASSES.contains(&class.as_str()),
        class,
        title: title_of(hwnd),
        visible: unsafe { IsWindowVisible(hwnd) }.as_bool(),
        rect: [r.left, r.top, r.right, r.bottom],
        is_own_process: pid == unsafe { GetCurrentProcessId() },
    }
}

pub fn foreground() -> Option<ForegroundInfo> {
    let hwnd = unsafe { GetForegroundWindow() };
    if hwnd.0.is_null() {
        None
    } else {
        Some(describe(hwnd))
    }
}

/// Actual top-level window, excluding owner chains (owned popups remain distinct).
pub fn root_of(hwnd: isize) -> Option<ForegroundInfo> {
    let root = unsafe { GetAncestor(HWND(hwnd as *mut _), GA_ROOT) };
    (!root.0.is_null()).then(|| describe(root))
}

/// False on the secure desktop (UAC prompt, lock screen) or when no interactive desktop exists.
pub fn input_desktop_accessible() -> bool {
    unsafe {
        match OpenInputDesktop(DESKTOP_CONTROL_FLAGS(0), false, DESKTOP_SWITCHDESKTOP) {
            Ok(h) => {
                let _ = CloseDesktop(h);
                true
            }
            Err(_) => false,
        }
    }
}

/// Why a target window must not receive the paste, if any.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Reject {
    NoForeground,
    NoInteractiveDesktop,
    NotVisible,
    OwnProcess,
    ShellWindow,
    TargetHigherIntegrity,
    UnknownProcessIdentity,
}

#[derive(Debug, Clone, Serialize)]
pub struct Admission {
    pub allowed: bool,
    pub reject: Option<Reject>,
    pub info: Option<ForegroundInfo>,
    pub relation: Option<integrity::Relation>,
    pub target_rid: Option<u32>,
}

/// Windows-level admission (UIA-level checks - password/read-only/disabled - live in `uia`).
pub fn admit_foreground() -> Admission {
    let deny = |reject: Reject, info: Option<ForegroundInfo>, relation, target_rid| Admission {
        allowed: false,
        reject: Some(reject),
        info,
        relation,
        target_rid,
    };
    if !input_desktop_accessible() {
        return deny(Reject::NoInteractiveDesktop, None, None, None);
    }
    let Some(info) = foreground() else {
        return deny(Reject::NoForeground, None, None, None);
    };
    if !info.visible {
        return deny(Reject::NotVisible, Some(info), None, None);
    }
    if info.is_own_process {
        return deny(Reject::OwnProcess, Some(info), None, None);
    }
    if info.is_shell {
        return deny(Reject::ShellWindow, Some(info), None, None);
    }
    if info.process_birth_100ns.is_none() {
        return deny(Reject::UnknownProcessIdentity, Some(info), None, None);
    }
    let (rel, rid) = integrity::compare_with_pid(info.pid);
    if !rel.injection_allowed() {
        return deny(Reject::TargetHigherIntegrity, Some(info), Some(rel), rid);
    }
    Admission {
        allowed: true,
        reject: None,
        info: Some(info),
        relation: Some(rel),
        target_rid: rid,
    }
}

struct Search {
    pid: u32,
    found: Vec<isize>,
}

unsafe extern "system" fn enum_cb(hwnd: HWND, lparam: LPARAM) -> BOOL {
    let search = &mut *(lparam.0 as *mut Search);
    let mut pid = 0u32;
    GetWindowThreadProcessId(hwnd, Some(&mut pid));
    if pid == search.pid
        && IsWindowVisible(hwnd).as_bool()
        && GetWindow(hwnd, GW_OWNER)
            .map(|o| o.0.is_null())
            .unwrap_or(true)
    {
        search.found.push(hwnd.0 as isize);
    }
    BOOL(1)
}

/// Visible, unowned top-level windows of a process.
pub fn windows_of_pid(pid: u32) -> Vec<isize> {
    let mut search = Search { pid, found: vec![] };
    unsafe {
        let _ = EnumWindows(Some(enum_cb), LPARAM(&mut search as *mut Search as isize));
    }
    search.found
}

pub fn wait_window_of_pid(pid: u32, timeout_ms: u64) -> Option<HWND> {
    let end = std::time::Instant::now() + std::time::Duration::from_millis(timeout_ms);
    while std::time::Instant::now() < end {
        if let Some(h) = windows_of_pid(pid).first() {
            return Some(HWND(*h as *mut _));
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    None
}

/// Test-fixture helper (NOT used by the product): make a window we launched the foreground window.
/// F24 is a best-effort activation aid; only the subsequent GetForegroundWindow checks prove focus.
pub fn bring_to_foreground(hwnd: HWND) -> R<bool> {
    let is_fg = || unsafe { GetForegroundWindow() } == hwnd;
    if is_fg() {
        return Ok(true);
    }
    crate::keys::tap(VIRTUAL_KEY(VK_F24.0));
    unsafe {
        let _ = SetForegroundWindow(hwnd);
    }
    for _ in 0..20 {
        if is_fg() {
            return Ok(true);
        }
        std::thread::sleep(std::time::Duration::from_millis(25));
    }
    Ok(is_fg())
}
