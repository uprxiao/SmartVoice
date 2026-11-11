//! Synchronous HWND lifetime witnesses. Numeric HWNDs and UIA provider identity
//! can both be recycled. An out-of-context WinEvent hook observes window
//! destruction without writing a property into the target process, so a busy
//! desktop heap or a Chromium window's exhausted property list cannot block it.
use crate::R;
use std::{
    collections::{BTreeMap, HashMap},
    rc::{Rc, Weak},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc, Arc, Mutex, OnceLock,
    },
    time::Duration,
};
use windows::Win32::{
    Foundation::{HWND, LPARAM, WPARAM},
    System::Threading::GetCurrentThreadId,
    UI::Accessibility::{SetWinEventHook, UnhookWinEvent, HWINEVENTHOOK},
    UI::WindowsAndMessaging::{
        GetGUIThreadInfo, GetMessageW, IsWindow, PeekMessageW, PostThreadMessageW, CHILDID_SELF,
        EVENT_OBJECT_DESTROY, GUITHREADINFO, MSG, OBJID_WINDOW, PM_NOREMOVE, WINEVENT_OUTOFCONTEXT,
        WM_QUIT,
    },
};

struct Entry {
    generation: u64,
    alive: Arc<AtomicBool>,
}

#[derive(Default)]
struct Registry {
    entries: Mutex<HashMap<isize, Entry>>,
    next: AtomicU64,
}

impl Registry {
    fn entries(&self) -> std::sync::MutexGuard<'_, HashMap<isize, Entry>> {
        self.entries
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn register(&self, hwnd: isize) -> (u64, Arc<AtomicBool>) {
        let generation = self.next.fetch_add(1, Ordering::SeqCst);
        let alive = Arc::new(AtomicBool::new(true));
        self.entries().insert(
            hwnd,
            Entry {
                generation,
                alive: alive.clone(),
            },
        );
        (generation, alive)
    }

    fn destroyed(&self, hwnd: isize) {
        if let Some(entry) = self.entries().remove(&hwnd) {
            entry.alive.store(false, Ordering::SeqCst);
        }
    }

    fn unregister(&self, hwnd: isize, generation: u64) {
        let mut entries = self.entries();
        if entries
            .get(&hwnd)
            .is_some_and(|entry| entry.generation == generation)
        {
            entries.remove(&hwnd);
        }
    }
}

static REGISTRY: OnceLock<Arc<Registry>> = OnceLock::new();
static WATCHER_STOPPED: AtomicBool = AtomicBool::new(false);

fn registry() -> &'static Arc<Registry> {
    REGISTRY.get_or_init(|| Arc::new(Registry::default()))
}

unsafe extern "system" fn destroyed(
    _hook: HWINEVENTHOOK,
    event: u32,
    hwnd: HWND,
    id_object: i32,
    id_child: i32,
    _thread: u32,
    _time: u32,
) {
    if event == EVENT_OBJECT_DESTROY
        && id_object == OBJID_WINDOW.0
        && id_child == CHILDID_SELF as i32
    {
        registry().destroyed(hwnd.0 as isize);
    }
}

struct LifetimeWatcher {
    thread_id: u32,
}

impl LifetimeWatcher {
    fn start() -> R<Self> {
        let (sender, receiver) = mpsc::sync_channel(1);
        std::thread::spawn(move || unsafe {
            let mut message = MSG::default();
            let _ = PeekMessageW(&mut message, None, 0, 0, PM_NOREMOVE);
            let hook = SetWinEventHook(
                EVENT_OBJECT_DESTROY,
                EVENT_OBJECT_DESTROY,
                None,
                Some(destroyed),
                0,
                0,
                WINEVENT_OUTOFCONTEXT,
            );
            if hook.0.is_null() {
                let _ = sender.send(Err("SetWinEventHook failed".to_string()));
                return;
            }
            let _ = sender.send(Ok(GetCurrentThreadId()));
            while GetMessageW(&mut message, None, 0, 0).0 > 0 {}
            WATCHER_STOPPED.store(true, Ordering::SeqCst);
            let _ = UnhookWinEvent(hook);
        });
        let thread_id = receiver
            .recv_timeout(Duration::from_secs(3))
            .map_err(|error| error.to_string())??;
        Ok(Self { thread_id })
    }
}

impl Drop for LifetimeWatcher {
    fn drop(&mut self) {
        unsafe {
            let _ = PostThreadMessageW(self.thread_id, WM_QUIT, WPARAM(0), LPARAM(0));
        }
    }
}

static WATCHER: OnceLock<Result<LifetimeWatcher, String>> = OnceLock::new();

fn ensure_watcher() -> R<()> {
    if WATCHER_STOPPED.load(Ordering::SeqCst) {
        return Err("target lifetime watcher stopped".into());
    }
    match WATCHER.get_or_init(LifetimeWatcher::start) {
        Ok(_) => Ok(()),
        Err(error) => Err(error.clone()),
    }
}

struct Witness {
    hwnd: isize,
    generation: u64,
    alive: Arc<AtomicBool>,
}

impl Witness {
    fn alive(&self) -> bool {
        !WATCHER_STOPPED.load(Ordering::SeqCst)
            && self.alive.load(Ordering::SeqCst)
            && unsafe { IsWindow(Some(HWND(self.hwnd as *mut _))) }.as_bool()
    }
}

impl Drop for Witness {
    fn drop(&mut self) {
        registry().unregister(self.hwnd, self.generation);
    }
}

pub struct Captured {
    witnesses: Vec<Rc<Witness>>,
    tid: u32,
    focused: HWND,
}

impl Captured {
    pub fn alive(&self) -> bool {
        self.witnesses.iter().all(|witness| witness.alive())
    }

    pub fn focus_unchanged(&self) -> bool {
        focused(self.tid).is_ok_and(|hwnd| hwnd == self.focused)
    }
}

fn focused(tid: u32) -> R<HWND> {
    let mut gui = GUITHREADINFO {
        cbSize: std::mem::size_of::<GUITHREADINFO>() as u32,
        ..Default::default()
    };
    unsafe { GetGUIThreadInfo(tid, &mut gui) }.map_err(|error| error.to_string())?;
    Ok(gui.hwndFocus)
}

#[derive(Default)]
pub struct Tracker {
    windows: BTreeMap<isize, Weak<Witness>>,
}

impl Tracker {
    pub fn capture(&mut self, root: HWND, tid: u32) -> R<Captured> {
        ensure_watcher()?;
        let focus = focused(tid)?;
        let mut handles = vec![root];
        if !focus.is_invalid() && focus != root {
            // Composite apps can host input in another process. The hook is
            // process-wide, so no cross-process property write is needed.
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
                .filter(|witness| witness.alive());
            let witness = match existing {
                Some(witness) => witness,
                None => {
                    let (generation, alive) = registry().register(key);
                    let witness = Rc::new(Witness {
                        hwnd: key,
                        generation,
                        alive,
                    });
                    self.windows.insert(key, Rc::downgrade(&witness));
                    witness
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};
    use windows::{
        core::w,
        Win32::UI::WindowsAndMessaging::{
            CreateWindowExW, DestroyWindow, WINDOW_EX_STYLE, WINDOW_STYLE,
        },
    };

    fn fixture() -> HWND {
        unsafe {
            CreateWindowExW(
                WINDOW_EX_STYLE::default(),
                w!("STATIC"),
                w!("lifetime fixture"),
                WINDOW_STYLE::default(),
                0,
                0,
                10,
                10,
                None,
                None,
                None,
                None,
            )
            .expect("fixture window")
        }
    }

    fn wait_until(mut condition: impl FnMut() -> bool) {
        let deadline = Instant::now() + Duration::from_secs(2);
        while !condition() {
            assert!(Instant::now() < deadline, "lifetime witness timed out");
            std::thread::sleep(Duration::from_millis(25));
        }
    }

    #[test]
    fn live_window_stays_alive_and_destroyed_window_turns_dead() {
        let mut tracker = Tracker::default();
        let hwnd = fixture();
        let captured = tracker
            .capture(hwnd, unsafe { GetCurrentThreadId() })
            .expect("capture live fixture");
        assert!(captured.alive());
        std::thread::sleep(Duration::from_millis(100));
        assert!(captured.alive());
        unsafe { DestroyWindow(hwnd) }.expect("destroy fixture");
        wait_until(|| !captured.alive());
    }
}
