//! Synchronous HWND lifetime witnesses. Numeric HWNDs and UIA provider identity
//! can both be recycled. A private property belongs to the window object and
//! disappears when that object is destroyed; a replacement cannot inherit it.
use crate::R;
use std::{
    collections::BTreeMap,
    rc::{Rc, Weak},
    sync::atomic::{AtomicU32, Ordering},
};
use windows::{
    core::w,
    Win32::{
        Foundation::{HANDLE, HWND},
        UI::WindowsAndMessaging::{
            GetGUIThreadInfo, GetPropW, GetWindowThreadProcessId, RemovePropW, SetPropW,
            GUITHREADINFO,
        },
    },
};

static NEXT: AtomicU32 = AtomicU32::new(1);
const NAME: windows::core::PCWSTR = w!("SmartVoiceInput.TargetLifecycle.v1");

struct Witness {
    hwnd: HWND,
    cookie: usize,
}
impl Witness {
    fn alive(&self) -> bool {
        unsafe { GetPropW(self.hwnd, NAME).0 as usize == self.cookie }
    }
}
impl Drop for Witness {
    fn drop(&mut self) {
        // Remove only our value. Never touch a new window/helper's witness.
        if self.alive() {
            unsafe {
                let _ = RemovePropW(self.hwnd, NAME);
            }
        }
    }
}
pub struct Captured {
    witnesses: Vec<Rc<Witness>>,
    tid: u32,
    focused: HWND,
}
impl Captured {
    pub fn alive(&self) -> bool {
        self.witnesses.iter().all(|w| w.alive())
    }
    pub fn focus_unchanged(&self) -> bool {
        focused(self.tid).is_ok_and(|h| h == self.focused)
    }
}
fn focused(tid: u32) -> R<HWND> {
    let mut gui = GUITHREADINFO {
        cbSize: std::mem::size_of::<GUITHREADINFO>() as u32,
        ..Default::default()
    };
    unsafe { GetGUIThreadInfo(tid, &mut gui) }.map_err(|e| e.to_string())?;
    Ok(gui.hwndFocus)
}
#[derive(Default)]
pub struct Tracker {
    windows: BTreeMap<isize, Weak<Witness>>,
}
impl Tracker {
    pub fn capture(&mut self, root: HWND, tid: u32, pid: u32) -> R<Captured> {
        let focus = focused(tid)?;
        let mut handles = vec![root];
        if !focus.is_invalid() && focus != root {
            let mut owner = 0;
            unsafe {
                GetWindowThreadProcessId(focus, Some(&mut owner));
            }
            if owner != pid {
                return Err("target focus process changed".into());
            }
            handles.push(focus);
        }
        self.windows.retain(|_, value| value.strong_count() > 0);
        let mut witnesses = Vec::new();
        for hwnd in handles {
            let key = hwnd.0 as isize;
            let existing = self
                .windows
                .get(&key)
                .and_then(Weak::upgrade)
                .filter(|w| w.alive());
            let witness = match existing {
                Some(value) => value,
                None => {
                    let serial = NEXT
                        .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_add(1))
                        .map_err(|_| "target lifetime capacity")?;
                    let cookie = ((std::process::id() as u64) << 32 | serial as u64) as usize;
                    let value = Rc::new(Witness { hwnd, cookie });
                    unsafe { SetPropW(hwnd, NAME, Some(HANDLE(cookie as *mut _))) }
                        .map_err(|e| e.to_string())?;
                    if !value.alive() {
                        return Err("target lifetime witness unavailable".into());
                    }
                    self.windows.insert(key, Rc::downgrade(&value));
                    value
                }
            };
            witnesses.push(witness);
        }
        let captured = Captured {
            witnesses,
            tid,
            focused: focus,
        };
        if !captured.alive() || !captured.focus_unchanged() {
            return Err("target changed during lifetime capture".into());
        }
        Ok(captured)
    }
}
