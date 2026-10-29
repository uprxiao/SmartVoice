//! UI Automation: target identification (password / read-only / disabled), text read-back with the
//! caret offset, and the caret-rectangle fallback chain used to anchor the recording capsule.
//!
//! Chromium/Electron build their UIA tree lazily: the first read from a new client can be empty and
//! later ones succeed, so callers use `focused_with_retry` and treat "cannot read" as `unverified`.
pub use crate::verification::Readback;
use crate::{ctx, R};
use serde::Serialize;
use windows::core::BSTR;
use windows::Win32::Foundation::{POINT, RECT};
use windows::Win32::Graphics::Gdi::ClientToScreen;
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CLSCTX_INPROC_SERVER, COINIT_MULTITHREADED, SAFEARRAY,
};
use windows::Win32::System::Ole::{
    SafeArrayAccessData, SafeArrayDestroy, SafeArrayGetLBound, SafeArrayGetUBound,
    SafeArrayUnaccessData,
};
use windows::Win32::UI::Accessibility::{
    CUIAutomation, IUIAutomation, IUIAutomationElement, IUIAutomationTextPattern,
    IUIAutomationTextRange, IUIAutomationValuePattern, TextPatternRangeEndpoint_End,
    TextPatternRangeEndpoint_Start, TextUnit_Character, UIA_TextPatternId, UIA_ValuePatternId,
};
use windows::Win32::UI::WindowsAndMessaging::{
    GetCursorPos, GetForegroundWindow, GetGUIThreadInfo, GetWindowThreadProcessId, GUITHREADINFO,
};

pub struct Uia {
    auto: IUIAutomation,
}

pub struct FocusedRead {
    pub element: IUIAutomationElement,
    pub readback: Option<Readback>,
    pub attempts: Vec<ReadAttempt>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ReadAttempt {
    pub elapsed_ms: f64,
    pub text_utf16_len: Option<usize>,
    pub has_caret: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct ElementInfo {
    pub name: String,
    pub class_name: String,
    pub control_type: i32,
    pub framework_id: String,
    pub pid: i32,
    pub is_password: bool,
    pub is_enabled: bool,
    pub has_keyboard_focus: bool,
    pub rect: [i32; 4],
    pub has_value_pattern: bool,
    pub value_readonly: Option<bool>,
    pub has_text_pattern: bool,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TargetClass {
    Ok,
    Password,
    ReadOnly,
    Disabled,
    /// Neither value nor text pattern: cannot verify, but the paste may still be attempted.
    Unverifiable,
}

pub fn classify(info: &ElementInfo) -> TargetClass {
    if info.is_password {
        TargetClass::Password
    } else if !info.is_enabled {
        TargetClass::Disabled
    } else if info.value_readonly == Some(true) {
        TargetClass::ReadOnly
    } else if !info.has_value_pattern && !info.has_text_pattern {
        TargetClass::Unverifiable
    } else {
        TargetClass::Ok
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct CaretStep {
    pub source: &'static str,
    pub rect: Option<[i32; 4]>,
    pub note: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct CaretFix {
    pub chosen: Option<&'static str>,
    pub rect: Option<[i32; 4]>,
    pub steps: Vec<CaretStep>,
}

fn non_empty(r: &[i32; 4]) -> bool {
    r[2] > r[0] || r[3] > r[1]
}

impl Uia {
    /// Preserve real empty/unreadable observations before the first usable text/caret read.
    pub fn readable_with_retry(&self, timeout_ms: u64) -> R<FocusedRead> {
        let start = std::time::Instant::now();
        let mut attempts = Vec::new();
        loop {
            let element = self.focused()?;
            let info = self.describe(&element);
            let readback = if info.is_password {
                None
            } else {
                self.read_back(&element).ok()
            };
            attempts.push(ReadAttempt {
                elapsed_ms: start.elapsed().as_secs_f64() * 1000.0,
                text_utf16_len: readback.as_ref().map(|r| r.text.encode_utf16().count()),
                has_caret: readback.as_ref().is_some_and(|r| r.caret_utf16.is_some()),
            });
            let ready = readback
                .as_ref()
                .is_some_and(|r| !r.text.is_empty() && r.caret_utf16.is_some());
            if ready || info.is_password || start.elapsed().as_millis() >= timeout_ms as u128 {
                return Ok(FocusedRead {
                    element,
                    readback,
                    attempts,
                });
            }
            std::thread::sleep(std::time::Duration::from_millis(150));
        }
    }

    pub fn new() -> R<Self> {
        unsafe {
            // S_OK / S_FALSE (already initialised) are fine; a different apartment model is tolerated.
            let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
            let auto: IUIAutomation = CoCreateInstance(&CUIAutomation, None, CLSCTX_INPROC_SERVER)
                .map_err(ctx("CoCreateInstance(CUIAutomation)"))?;
            Ok(Uia { auto })
        }
    }

    pub fn focused(&self) -> R<IUIAutomationElement> {
        unsafe {
            self.auto
                .GetFocusedElement()
                .map_err(ctx("GetFocusedElement"))
        }
    }

    pub fn at_point(&self, x: i32, y: i32) -> R<IUIAutomationElement> {
        unsafe {
            self.auto
                .ElementFromPoint(POINT { x, y })
                .map_err(ctx("ElementFromPoint"))
        }
    }

    pub fn same_element(&self, a: &IUIAutomationElement, b: &IUIAutomationElement) -> bool {
        unsafe {
            self.auto
                .CompareElements(a, b)
                .map(|v| v.as_bool())
                .unwrap_or(false)
        }
    }

    /// Chromium may need a moment (and a nudge) before it exposes its tree to a new UIA client.
    /// Returns the element together with how many attempts it took.
    pub fn focused_with_retry(&self, timeout_ms: u64) -> R<(IUIAutomationElement, u32)> {
        let end = std::time::Instant::now() + std::time::Duration::from_millis(timeout_ms);
        let mut attempts = 0;
        let mut last_err = String::new();
        loop {
            attempts += 1;
            match self.focused() {
                Ok(el) => {
                    let info = self.describe(&el);
                    if info.has_value_pattern
                        || info.has_text_pattern
                        || std::time::Instant::now() >= end
                    {
                        return Ok((el, attempts));
                    }
                }
                Err(e) => last_err = e,
            }
            if std::time::Instant::now() >= end {
                return Err(format!(
                    "no readable focused element after {attempts} attempts: {last_err}"
                ));
            }
            std::thread::sleep(std::time::Duration::from_millis(150));
        }
    }

    pub fn describe(&self, el: &IUIAutomationElement) -> ElementInfo {
        unsafe {
            let s = |r: windows::core::Result<BSTR>| r.map(|b| b.to_string()).unwrap_or_default();
            let rect = el.CurrentBoundingRectangle().unwrap_or(RECT::default());
            let value = el
                .GetCurrentPatternAs::<IUIAutomationValuePattern>(UIA_ValuePatternId)
                .ok();
            let value_readonly = value
                .as_ref()
                .and_then(|v| v.CurrentIsReadOnly().ok())
                .map(|b| b.as_bool());
            ElementInfo {
                name: s(el.CurrentName()),
                class_name: s(el.CurrentClassName()),
                control_type: el.CurrentControlType().map(|c| c.0).unwrap_or(0),
                framework_id: s(el.CurrentFrameworkId()),
                pid: el.CurrentProcessId().unwrap_or(0),
                is_password: el.CurrentIsPassword().map(|b| b.as_bool()).unwrap_or(false),
                is_enabled: el.CurrentIsEnabled().map(|b| b.as_bool()).unwrap_or(true),
                has_keyboard_focus: el
                    .CurrentHasKeyboardFocus()
                    .map(|b| b.as_bool())
                    .unwrap_or(false),
                rect: [rect.left, rect.top, rect.right, rect.bottom],
                has_value_pattern: value.is_some(),
                value_readonly,
                has_text_pattern: el
                    .GetCurrentPatternAs::<IUIAutomationTextPattern>(UIA_TextPatternId)
                    .is_ok(),
            }
        }
    }

    /// Text plus caret offset. Prefers the text pattern (it carries the selection); falls back to the value pattern.
    pub fn read_back(&self, el: &IUIAutomationElement) -> R<Readback> {
        unsafe {
            if let Ok(tp) = el.GetCurrentPatternAs::<IUIAutomationTextPattern>(UIA_TextPatternId) {
                let doc = tp.DocumentRange().map_err(ctx("DocumentRange"))?;
                let text = doc.GetText(-1).map_err(ctx("GetText"))?.to_string();
                let (caret, sel_len) = selection_of(&tp, &doc);
                return Ok(Readback {
                    source: "text_pattern".into(),
                    text,
                    caret_utf16: caret,
                    selection_utf16_len: sel_len,
                });
            }
            let vp = el
                .GetCurrentPatternAs::<IUIAutomationValuePattern>(UIA_ValuePatternId)
                .map_err(ctx("no text/value pattern"))?;
            let text = vp.CurrentValue().map_err(ctx("CurrentValue"))?.to_string();
            Ok(Readback {
                source: "value_pattern".into(),
                text,
                caret_utf16: None,
                selection_utf16_len: None,
            })
        }
    }

    /// Every step of the caret chain is evaluated (not short-circuited) so a verification run can see
    /// which sources work for a given app; `chosen` is the first usable one in priority order.
    pub fn caret_chain(&self, el: Option<&IUIAutomationElement>) -> CaretFix {
        let mut steps = vec![];
        // 1. Classic Win32 caret (GetGUIThreadInfo): empty for Chromium/Electron/new Notepad/Windows Terminal.
        steps.push(guithread_caret());
        // 2. UIA text pattern: selection range -> bounding rectangles, expanding a degenerate range by one character.
        steps.push(match el {
            Some(e) => self.uia_text_caret(e),
            None => CaretStep {
                source: "uia_text",
                rect: None,
                note: "no element".into(),
            },
        });
        // 3. Focused element's own bounds.
        steps.push(match el {
            Some(e) => {
                let r = unsafe { e.CurrentBoundingRectangle() }.unwrap_or_default();
                let a = [r.left, r.top, r.right, r.bottom];
                CaretStep {
                    source: "element_bounds",
                    rect: non_empty(&a).then_some(a),
                    note: String::new(),
                }
            }
            None => CaretStep {
                source: "element_bounds",
                rect: None,
                note: "no element".into(),
            },
        });
        // 4. Mouse position: last resort so the capsule is never left unanchored.
        let mut p = POINT::default();
        let ok = unsafe { GetCursorPos(&mut p) }.is_ok();
        steps.push(CaretStep {
            source: "cursor",
            rect: ok.then_some([p.x, p.y, p.x, p.y]),
            note: String::new(),
        });
        let chosen = steps
            .iter()
            .find(|s| s.source != "cursor" && s.rect.is_some())
            .or_else(|| steps.last());
        CaretFix {
            chosen: chosen.map(|s| s.source),
            rect: chosen.and_then(|s| s.rect),
            steps,
        }
    }

    fn uia_text_caret(&self, el: &IUIAutomationElement) -> CaretStep {
        let fail = |note: &str| CaretStep {
            source: "uia_text",
            rect: None,
            note: note.into(),
        };
        unsafe {
            let Ok(tp) = el.GetCurrentPatternAs::<IUIAutomationTextPattern>(UIA_TextPatternId)
            else {
                return fail("no text pattern");
            };
            let Ok(sel) = tp.GetSelection() else {
                return fail("GetSelection failed");
            };
            if sel.Length().unwrap_or(0) == 0 {
                return fail("no selection range");
            }
            let Ok(range) = sel.GetElement(0) else {
                return fail("GetElement(0) failed");
            };
            let first = rects_of(&range);
            if let Some(r) = first.first() {
                return CaretStep {
                    source: "uia_text",
                    rect: Some(to_i32(r)),
                    note: "direct".into(),
                };
            }
            // A caret is a zero-length range; its rectangle list is usually empty. Expand by one character.
            let Ok(clone) = range.Clone() else {
                return fail("Clone failed");
            };
            let _ = clone.ExpandToEnclosingUnit(TextUnit_Character);
            let expanded = rects_of(&clone);
            match expanded.first() {
                Some(r) => CaretStep {
                    source: "uia_text",
                    rect: Some(to_i32(r)),
                    note: "expanded_one_character".into(),
                },
                None => fail("empty rectangles even after expanding one character"),
            }
        }
    }
}

fn to_i32(r: &[f64; 4]) -> [i32; 4] {
    // UIA gives (left, top, width, height) in physical pixels.
    [
        r[0] as i32,
        r[1] as i32,
        (r[0] + r[2]) as i32,
        (r[1] + r[3]) as i32,
    ]
}

unsafe fn rects_of(range: &IUIAutomationTextRange) -> Vec<[f64; 4]> {
    let Ok(psa) = range.GetBoundingRectangles() else {
        return vec![];
    };
    safearray_rects(psa)
}

unsafe fn safearray_rects(psa: *mut SAFEARRAY) -> Vec<[f64; 4]> {
    if psa.is_null() {
        return vec![];
    }
    let lb = SafeArrayGetLBound(psa, 1).unwrap_or(0);
    let ub = SafeArrayGetUBound(psa, 1).unwrap_or(-1);
    let n = if ub >= lb { (ub - lb + 1) as usize } else { 0 };
    let mut out = vec![];
    if n >= 4 {
        let mut data: *mut std::ffi::c_void = std::ptr::null_mut();
        if SafeArrayAccessData(psa, &mut data).is_ok() && !data.is_null() {
            let s = std::slice::from_raw_parts(data as *const f64, n);
            for c in s.chunks_exact(4) {
                out.push([c[0], c[1], c[2], c[3]]);
            }
            let _ = SafeArrayUnaccessData(psa);
        }
    }
    let _ = SafeArrayDestroy(psa);
    out
}

fn selection_of(
    tp: &IUIAutomationTextPattern,
    doc: &IUIAutomationTextRange,
) -> (Option<usize>, Option<usize>) {
    unsafe {
        let Ok(sel) = tp.GetSelection() else {
            return (None, None);
        };
        if sel.Length().unwrap_or(0) == 0 {
            return (None, None);
        }
        let Ok(r0) = sel.GetElement(0) else {
            return (None, None);
        };
        let sel_len = r0
            .GetText(-1)
            .map(|b| b.to_string().encode_utf16().count())
            .ok();
        // Text from document start up to the selection start = caret offset.
        let Ok(prefix) = doc.Clone() else {
            return (None, sel_len);
        };
        if prefix
            .MoveEndpointByRange(
                TextPatternRangeEndpoint_End,
                &r0,
                TextPatternRangeEndpoint_Start,
            )
            .is_err()
        {
            return (None, sel_len);
        }
        let caret = prefix
            .GetText(-1)
            .map(|b| b.to_string().encode_utf16().count())
            .ok();
        (caret, sel_len)
    }
}

fn guithread_caret() -> CaretStep {
    unsafe {
        let fg = GetForegroundWindow();
        if fg.0.is_null() {
            return CaretStep {
                source: "guithread",
                rect: None,
                note: "no foreground".into(),
            };
        }
        let tid = GetWindowThreadProcessId(fg, None);
        let mut info = GUITHREADINFO {
            cbSize: std::mem::size_of::<GUITHREADINFO>() as u32,
            ..Default::default()
        };
        if GetGUIThreadInfo(tid, &mut info).is_err() {
            return CaretStep {
                source: "guithread",
                rect: None,
                note: "GetGUIThreadInfo failed".into(),
            };
        }
        let rc = info.rcCaret;
        let empty = rc.right <= rc.left && rc.bottom <= rc.top;
        if info.hwndCaret.0.is_null() || empty {
            return CaretStep {
                source: "guithread",
                rect: None,
                note: format!(
                    "hwndCaret null={} rect empty={}",
                    info.hwndCaret.0.is_null(),
                    empty
                ),
            };
        }
        let mut tl = POINT {
            x: rc.left,
            y: rc.top,
        };
        let mut br = POINT {
            x: rc.right,
            y: rc.bottom,
        };
        let _ = ClientToScreen(info.hwndCaret, &mut tl);
        let _ = ClientToScreen(info.hwndCaret, &mut br);
        CaretStep {
            source: "guithread",
            rect: Some([tl.x, tl.y, br.x, br.y]),
            note: String::new(),
        }
    }
}
