//! Keyboard injection and modifier handling.
//!
//! If the user still holds the dictation shortcut's modifiers when we inject Ctrl+V, the target
//! sees Ctrl+Shift+V ("paste as plain text" in many apps, a terminal's own paste chord). So we wait
//! for release (<= ~500 ms), then neutralise whatever is still held, and only then inject.
use windows::Win32::UI::Input::KeyboardAndMouse::{
    GetAsyncKeyState, MapVirtualKeyW, SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT,
    KEYBD_EVENT_FLAGS, KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, MAPVK_VK_TO_VSC, VIRTUAL_KEY,
    VK_CONTROL, VK_LCONTROL, VK_LMENU, VK_LSHIFT, VK_LWIN, VK_MENU, VK_RCONTROL, VK_RMENU,
    VK_RSHIFT, VK_RWIN, VK_SHIFT, VK_V,
};

pub const MODIFIERS: [VIRTUAL_KEY; 8] = [
    VK_LCONTROL,
    VK_RCONTROL,
    VK_LSHIFT,
    VK_RSHIFT,
    VK_LMENU,
    VK_RMENU,
    VK_LWIN,
    VK_RWIN,
];

fn extended(vk: VIRTUAL_KEY) -> bool {
    // Arrow/navigation block and the right-hand / Windows modifiers need the extended-key flag.
    matches!(vk.0, 0x21..=0x28 | 0x2D | 0x2E)
        || [VK_RCONTROL, VK_RMENU, VK_LWIN, VK_RWIN].contains(&vk)
}

pub fn key_input(vk: VIRTUAL_KEY, up: bool) -> INPUT {
    let scan = unsafe { MapVirtualKeyW(vk.0 as u32, MAPVK_VK_TO_VSC) } as u16;
    let mut flags = KEYBD_EVENT_FLAGS(0);
    if up {
        flags |= KEYEVENTF_KEYUP;
    }
    if extended(vk) {
        flags |= KEYEVENTF_EXTENDEDKEY;
    }
    INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 {
            ki: KEYBDINPUT {
                wVk: vk,
                wScan: scan,
                dwFlags: flags,
                time: 0,
                dwExtraInfo: 0,
            },
        },
    }
}

/// Returns how many events the system *accepted*. UIPI-dropped input is still counted as accepted,
/// so this number is never proof of delivery.
pub fn send_inputs(inputs: &[INPUT]) -> u32 {
    unsafe { SendInput(inputs, std::mem::size_of::<INPUT>() as i32) }
}

pub fn tap(vk: VIRTUAL_KEY) -> u32 {
    send_inputs(&[key_input(vk, false), key_input(vk, true)])
}

pub fn down(vk: VIRTUAL_KEY) -> u32 {
    send_inputs(&[key_input(vk, false)])
}

pub fn up(vk: VIRTUAL_KEY) -> u32 {
    send_inputs(&[key_input(vk, true)])
}

/// Press keys in order and release in reverse (a chord such as Ctrl+Shift+Space).
pub fn chord(vks: &[VIRTUAL_KEY]) -> u32 {
    let mut seq: Vec<INPUT> = vks.iter().map(|k| key_input(*k, false)).collect();
    seq.extend(vks.iter().rev().map(|k| key_input(*k, true)));
    send_inputs(&seq)
}

pub fn send_ctrl_v() -> u32 {
    chord(&[VK_CONTROL, VK_V])
}

pub fn is_down(vk: VIRTUAL_KEY) -> bool {
    (unsafe { GetAsyncKeyState(vk.0 as i32) } as u16) & 0x8000 != 0
}

pub fn held_modifiers() -> Vec<u16> {
    MODIFIERS
        .iter()
        .filter(|k| is_down(**k))
        .map(|k| k.0)
        .collect()
}

/// True if all modifiers were released within the timeout.
pub fn wait_modifiers_released(timeout_ms: u64) -> bool {
    let end = std::time::Instant::now() + std::time::Duration::from_millis(timeout_ms);
    loop {
        if held_modifiers().is_empty() {
            return true;
        }
        if std::time::Instant::now() >= end {
            return false;
        }
        std::thread::sleep(std::time::Duration::from_millis(5));
    }
}

/// Inject key-up for every modifier still reported down; returns what was neutralised.
pub fn release_held_modifiers() -> Vec<u16> {
    let held = held_modifiers();
    let ups: Vec<INPUT> = held
        .iter()
        .map(|vk| key_input(VIRTUAL_KEY(*vk), true))
        .collect();
    if !ups.is_empty() {
        send_inputs(&ups);
    }
    held
}

/// Keep the generic names referenced so the neutralisation list stays in sync with `MODIFIERS`.
pub const GENERIC_MODIFIERS: [VIRTUAL_KEY; 3] = [VK_CONTROL, VK_SHIFT, VK_MENU];
