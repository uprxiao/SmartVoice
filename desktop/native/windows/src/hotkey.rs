//! Global hotkey registration probe. Electron\'s `globalShortcut` is a wrapper over `RegisterHotKey`:
//! it reports presses (no release). This probe registers the selected combinations and optionally
//! injects them to observe WM_HOTKEY. Registration is not evidence of Chinese IME compatibility;
//! real keyboard/input-method behavior must still be checked on the user's Windows machine.
use crate::keys;
use serde::Serialize;
use windows::Win32::UI::Input::KeyboardAndMouse::{
    RegisterHotKey, UnregisterHotKey, HOT_KEY_MODIFIERS, MOD_ALT, MOD_CONTROL, MOD_NOREPEAT,
    MOD_SHIFT, VIRTUAL_KEY, VK_CONTROL, VK_MENU, VK_SHIFT, VK_SPACE,
};
use windows::Win32::UI::WindowsAndMessaging::{PeekMessageW, MSG, PM_REMOVE, WM_HOTKEY};

#[derive(Debug, Clone, Serialize)]
pub struct HotkeyProbe {
    pub combo: String,
    pub registered: bool,
    pub last_error: Option<u32>,
    pub fired_when_injected: Option<bool>,
}

pub struct Combo {
    pub label: &'static str,
    pub mods: HOT_KEY_MODIFIERS,
    pub vk: VIRTUAL_KEY,
    pub chord: Vec<VIRTUAL_KEY>,
}

pub fn standard_combos() -> Vec<Combo> {
    vec![
        Combo {
            label: "Ctrl+Shift+Space",
            mods: MOD_CONTROL | MOD_SHIFT,
            vk: VK_SPACE,
            chord: vec![VK_CONTROL, VK_SHIFT, VK_SPACE],
        },
        Combo {
            label: "Ctrl+Alt+Space",
            mods: MOD_CONTROL | MOD_ALT,
            vk: VK_SPACE,
            chord: vec![VK_CONTROL, VK_MENU, VK_SPACE],
        },
        Combo {
            label: "Shift+Space",
            mods: MOD_SHIFT,
            vk: VK_SPACE,
            chord: vec![VK_SHIFT, VK_SPACE],
        },
        Combo {
            label: "Ctrl+Space",
            mods: MOD_CONTROL,
            vk: VK_SPACE,
            chord: vec![VK_CONTROL, VK_SPACE],
        },
    ]
}

/// Must run on the thread that owns the message queue (RegisterHotKey with a null hwnd posts to it).
pub fn probe(combo: &Combo, inject: bool) -> HotkeyProbe {
    unsafe {
        let id = 0x5A01;
        let reg = RegisterHotKey(None, id, combo.mods | MOD_NOREPEAT, combo.vk.0 as u32);
        let mut out = HotkeyProbe {
            combo: combo.label.into(),
            registered: reg.is_ok(),
            last_error: None,
            fired_when_injected: None,
        };
        match reg {
            Err(e) => out.last_error = Some((e.code().0 as u32) & 0xFFFF),
            Ok(()) => {
                if inject {
                    keys::chord(&combo.chord);
                    let end = std::time::Instant::now() + std::time::Duration::from_millis(1500);
                    let mut fired = false;
                    let mut msg = MSG::default();
                    while std::time::Instant::now() < end && !fired {
                        while PeekMessageW(&mut msg, None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE)
                            .as_bool()
                        {
                            if msg.message == WM_HOTKEY && msg.wParam.0 == id as usize {
                                fired = true;
                            }
                        }
                        std::thread::sleep(std::time::Duration::from_millis(10));
                    }
                    out.fired_when_injected = Some(fired);
                }
                let _ = UnregisterHotKey(None, id);
            }
        }
        out
    }
}
