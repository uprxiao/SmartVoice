//! Probe of physical modifier tracking. The hook observes only the eight modifier keys;
//! it retains no other keys, text, timestamps or target content, and forwards every event.
use crate::{keys, modifier_policy::State, R};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::mpsc;
use windows::Win32::Foundation::{HINSTANCE, LPARAM, LRESULT, WPARAM};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::System::Threading::GetCurrentThreadId;
use windows::Win32::UI::Input::KeyboardAndMouse::VIRTUAL_KEY;
use windows::Win32::UI::WindowsAndMessaging::{
    CallNextHookEx, GetMessageW, PeekMessageW, PostThreadMessageW, SetWindowsHookExW,
    UnhookWindowsHookEx, KBDLLHOOKSTRUCT, LLKHF_INJECTED, MSG, PM_NOREMOVE, WH_KEYBOARD_LL,
    WM_KEYDOWN, WM_KEYUP, WM_QUIT, WM_SYSKEYDOWN, WM_SYSKEYUP,
};

static STATE: AtomicU32 = AtomicU32::new(0);

fn bit(vk: u32) -> u16 {
    keys::MODIFIERS
        .iter()
        .position(|v| v.0 as u32 == vk)
        .map(|n| 1 << n)
        .unwrap_or(0)
}

unsafe extern "system" fn callback(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code >= 0 {
        let event = &*(lparam.0 as *const KBDLLHOOKSTRUCT);
        let mask = bit(event.vkCode);
        let down = match wparam.0 as u32 {
            WM_KEYDOWN | WM_SYSKEYDOWN => Some(true),
            WM_KEYUP | WM_SYSKEYUP => Some(false),
            _ => None,
        };
        if let Some(down) = down {
            let injected = event.flags & LLKHF_INJECTED != Default::default();
            let _ = STATE.fetch_update(Ordering::SeqCst, Ordering::SeqCst, |old| {
                Some(State::from_packed(old).event(mask, down, injected).packed())
            });
        }
    }
    CallNextHookEx(None, code, wparam, lparam)
}

pub struct Watcher {
    thread_id: u32,
}

impl Watcher {
    pub fn start() -> R<Self> {
        let (sender, receiver) = mpsc::sync_channel(1);
        std::thread::spawn(move || unsafe {
            let mut msg = MSG::default();
            let _ = PeekMessageW(&mut msg, None, 0, 0, PM_NOREMOVE);
            let initial = keys::held_modifiers()
                .iter()
                .fold(0, |m, vk| m | bit(*vk as u32));
            STATE.store(State::initial(initial).packed(), Ordering::SeqCst);
            let module = match GetModuleHandleW(None) {
                Ok(module) => module,
                Err(error) => {
                    let _ = sender.send(Err(error.to_string()));
                    return;
                }
            };
            let hook = match SetWindowsHookExW(
                WH_KEYBOARD_LL,
                Some(callback),
                Some(HINSTANCE(module.0)),
                0,
            ) {
                Ok(hook) => hook,
                Err(error) => {
                    let _ = sender.send(Err(error.to_string()));
                    return;
                }
            };
            let _ = sender.send(Ok(GetCurrentThreadId()));
            while GetMessageW(&mut msg, None, 0, 0).0 > 0 {}
            let _ = UnhookWindowsHookEx(hook);
        });
        let thread_id = receiver
            .recv_timeout(std::time::Duration::from_secs(3))
            .map_err(|e| e.to_string())??;
        Ok(Self { thread_id })
    }
}

impl Drop for Watcher {
    fn drop(&mut self) {
        unsafe {
            let _ = PostThreadMessageW(self.thread_id, WM_QUIT, WPARAM(0), LPARAM(0));
        }
    }
}

/// An already-held key at hook startup has unknown provenance; decline rather than invent it.
pub fn has_unknown_held() -> bool {
    let held = keys::held_modifiers()
        .iter()
        .fold(0, |mask, vk| mask | bit(*vk as u32));
    State::from_packed(STATE.load(Ordering::SeqCst)).has_unknown_held(held)
}

pub fn restore_still_physically_held(neutralised: &[u16]) -> Vec<u16> {
    let mut restored = Vec::new();
    for vk in neutralised {
        if State::from_packed(STATE.load(Ordering::SeqCst)).restore_mask(bit(*vk as u32)) != 0 {
            keys::down(VIRTUAL_KEY(*vk));
            restored.push(*vk);
        }
    }
    restored
}
