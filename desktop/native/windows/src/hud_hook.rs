//! Low-level mouse hook for the recording HUD.
//!
//! Windows may consume the first click on the non-activating HUD for window
//! activation, which leaves the cancel/confirm buttons unresponsive until a
//! second click. This hook observes only left-button down/up events while the
//! HUD capsule is visible, emits a fixed `cancel`/`confirm` action when the
//! press is inside one of the two capsule buttons, and swallows that press so
//! the HUD never needs to be activated. No coordinates, titles of other
//! windows, or clipboard content are logged.
#[cfg(windows)]
mod imp {
    use std::io::Write;
    use std::sync::atomic::{AtomicBool, Ordering};
    use windows::core::BOOL;
    use windows::Win32::Foundation::{HINSTANCE, HWND, LPARAM, LRESULT, POINT, RECT, WPARAM};
    use windows::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows::Win32::UI::HiDpi::{
        SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
    };
    use windows::Win32::UI::WindowsAndMessaging::{
        CallNextHookEx, DispatchMessageW, EnumWindows, GetCursorPos, GetMessageW, GetWindowRect,
        GetWindowTextW, IsWindowVisible, SetWindowsHookExW, TranslateMessage, UnhookWindowsHookEx,
        MSG, WH_MOUSE_LL, WM_LBUTTONDOWN, WM_LBUTTONUP,
    };

    static SWALLOW_UP: AtomicBool = AtomicBool::new(false);

    // Geometry mirrors desktop/src/renderer/hud.css.
    const WINDOW_WIDTH_CSS: f64 = 300.0;
    const PILL_WIDTH_CSS: f64 = 116.0;
    const PILL_HEIGHT_CSS: f64 = 34.0;
    const PILL_PADDING_CSS: f64 = 4.0;
    const BUTTON_CSS: f64 = 24.0;
    const WAVE_WIDTH_CSS: f64 = 38.0;
    const WAVE_MARGIN_CSS: f64 = 8.0;
    const WRAP_PADDING_CSS: f64 = 8.0;

    fn window_title(hwnd: HWND) -> String {
        let mut buffer = [0u16; 256];
        let length = unsafe { GetWindowTextW(hwnd, &mut buffer) };
        String::from_utf16_lossy(&buffer[..length.max(0) as usize])
    }

    fn find_hud() -> Option<HWND> {
        unsafe extern "system" fn callback(hwnd: HWND, lparam: LPARAM) -> BOOL {
            let best = &mut *(lparam.0 as *mut Option<(HWND, i32)>);
            if !unsafe { IsWindowVisible(hwnd) }.as_bool() {
                return BOOL(1);
            }
            if !window_title(hwnd).contains("录音") {
                return BOOL(1);
            }
            let mut rect = RECT::default();
            if unsafe { GetWindowRect(hwnd, &mut rect) }.is_err() {
                return BOOL(1);
            }
            let width = rect.right - rect.left;
            let height = rect.bottom - rect.top;
            // The HUD is the small 300x104 CSS capsule window. The main window
            // is much larger and may share the title fragment while recording.
            if width > 0 && height > 0 && width <= 800 && height <= 500 {
                if best.map_or(true, |(_, best_width)| best_width > width) {
                    *best = Some((hwnd, width));
                }
            }
            BOOL(1)
        }
        let mut best: Option<(HWND, i32)> = None;
        unsafe {
            let _ = EnumWindows(Some(callback), LPARAM(&mut best as *mut _ as isize));
        }
        best.map(|(hwnd, _)| hwnd)
    }

    fn hit_action(point: POINT) -> Option<&'static str> {
        let hud = find_hud()?;
        if !unsafe { IsWindowVisible(hud) }.as_bool() {
            return None;
        }
        let mut rect = RECT::default();
        unsafe { GetWindowRect(hud, &mut rect) }.ok()?;
        let width = (rect.right - rect.left) as f64;
        if width <= 0.0 {
            return None;
        }
        let scale = width / WINDOW_WIDTH_CSS;
        let px = point.x as f64;
        let py = point.y as f64;
        let pill_left = rect.left as f64 + (WINDOW_WIDTH_CSS - PILL_WIDTH_CSS) / 2.0 * scale;
        let pill_bottom = rect.bottom as f64 - WRAP_PADDING_CSS * scale;
        let pill_top = pill_bottom - PILL_HEIGHT_CSS * scale;
        let button_top = pill_top + PILL_PADDING_CSS * scale;
        let button_bottom = button_top + BUTTON_CSS * scale;
        if py < button_top || py > button_bottom {
            return None;
        }
        let cancel_left = pill_left + PILL_PADDING_CSS * scale;
        let cancel_right = cancel_left + BUTTON_CSS * scale;
        if px >= cancel_left && px <= cancel_right {
            return Some("cancel");
        }
        let confirm_left =
            cancel_right + (WAVE_MARGIN_CSS + WAVE_WIDTH_CSS + WAVE_MARGIN_CSS) * scale;
        let confirm_right = confirm_left + BUTTON_CSS * scale;
        if px >= confirm_left && px <= confirm_right {
            return Some("confirm");
        }
        None
    }

    unsafe extern "system" fn hook_proc(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
        if code < 0 {
            return unsafe { CallNextHookEx(None, code, wparam, lparam) };
        }
        let message = wparam.0 as u32;
        if message == WM_LBUTTONDOWN {
            // Use the same DPI-aware coordinate space as GetWindowRect. The
            // low-level hook's MSLLHOOKSTRUCT point is physical, while this
            // process may still be DPI-virtualized.
            let mut point = POINT::default();
            if unsafe { GetCursorPos(&mut point) }.is_ok() {
                if let Some(action) = hit_action(point) {
                    let mut out = std::io::stdout().lock();
                    let _ = writeln!(out, "{{\"action\":\"{action}\"}}");
                    let _ = out.flush();
                    SWALLOW_UP.store(true, Ordering::SeqCst);
                    return LRESULT(1);
                }
            }
        } else if message == WM_LBUTTONUP && SWALLOW_UP.swap(false, Ordering::SeqCst) {
            return LRESULT(1);
        }
        unsafe { CallNextHookEx(None, code, wparam, lparam) }
    }

    pub fn run() -> Result<(), String> {
        unsafe {
            let _ = SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
        }
        let module = unsafe { GetModuleHandleW(None) }.map_err(|error| error.to_string())?;
        let hook = unsafe {
            SetWindowsHookExW(WH_MOUSE_LL, Some(hook_proc), Some(HINSTANCE(module.0)), 0)
        }
        .map_err(|error| error.to_string())?;
        let mut message = MSG::default();
        loop {
            let result = unsafe { GetMessageW(&mut message, None, 0, 0) };
            if result.0 <= 0 {
                break;
            }
            unsafe {
                let _ = TranslateMessage(&message);
                DispatchMessageW(&message);
            }
        }
        unsafe {
            let _ = UnhookWindowsHookEx(hook);
        }
        Ok(())
    }
}

#[cfg(windows)]
pub fn run() -> Result<(), String> {
    imp::run()
}
