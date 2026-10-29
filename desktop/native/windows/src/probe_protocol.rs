//! JSONL actions used only by the Windows feasibility harness, not the product RPC.
use crate::{foreground, hotkey, integrity, keys, paste, uia::Uia, R};
use serde_json::{json, Value};
use windows::Win32::Foundation::HWND;
use windows::Win32::UI::Input::KeyboardAndMouse::{VK_CONTROL, VK_HOME, VK_RIGHT, VK_SHIFT};

fn fixture_pid(request: &Value) -> R<u32> {
    request["fixture_pid"]
        .as_u64()
        .and_then(|v| u32::try_from(v).ok())
        .filter(|p| *p != 0)
        .ok_or_else(|| "explicit nonzero fixture_pid required".into())
}

pub fn dispatch(uia: &Uia, request: &Value) -> R<Value> {
    if std::env::var("SVI_WINDOWS_PROBE").as_deref() != Ok("1") {
        return Err("technical probe requires SVI_WINDOWS_PROBE=1".into());
    }
    let action = request["action"].as_str().ok_or("action required")?;
    match action {
        "ping" => Ok(
            json!({"probe_only":true,"pid":std::process::id(),"integrity_rid":integrity::own_rid()?,
            "executable":std::env::current_exe().map_err(|e| e.to_string())?}),
        ),
        "foreground" => {
            use windows::Win32::UI::WindowsAndMessaging::{GetAncestor, GA_ROOT, GA_ROOTOWNER};
            let root = foreground::foreground()
                .map(|f| unsafe { GetAncestor(HWND(f.hwnd as *mut _), GA_ROOT) })
                .filter(|h| !h.0.is_null())
                .map(foreground::describe);
            let root_owner = foreground::foreground()
                .map(|f| unsafe { GetAncestor(HWND(f.hwnd as *mut _), GA_ROOTOWNER) })
                .filter(|h| !h.0.is_null())
                .map(foreground::describe);
            Ok(
                json!({"admission":foreground::admit_foreground(),"root":root,"root_owner":root_owner}),
            )
        }
        "probeProcessLife" => {
            use windows::Win32::Foundation::{CloseHandle, FILETIME};
            use windows::Win32::System::Threading::{
                GetProcessTimes, OpenProcess, WaitForSingleObject,
                PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SYNCHRONIZE,
            };
            let pid = fixture_pid(request)?;
            unsafe {
                let process = OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
                    false,
                    pid,
                )
                .map_err(|e| e.to_string())?;
                let (mut birth, mut exit, mut kernel, mut user) = (
                    FILETIME::default(),
                    FILETIME::default(),
                    FILETIME::default(),
                    FILETIME::default(),
                );
                let times =
                    GetProcessTimes(process, &mut birth, &mut exit, &mut kernel, &mut user).is_ok();
                let state = WaitForSingleObject(process, 0).0;
                let _ = CloseHandle(process);
                Ok(
                    json!({"creation_time_available":times,"wait_state":state,"live_birth":integrity::process_birth(pid)}),
                )
            }
        }
        "probeRestoreControl" => {
            if std::env::var("GITHUB_ACTIONS").as_deref() != Ok("true") {
                return Err("restore control requires disposable CI fixtures".into());
            }
            let pid = fixture_pid(request)?;
            let parent = request["fixture_parent_pid"]
                .as_u64()
                .and_then(|p| u32::try_from(p).ok())
                .filter(|p| *p != 0 && *p != pid)
                .ok_or("distinct fixture parent required")?;
            let root = foreground::wait_window_of_pid(pid, 5000).ok_or("target root not found")?;
            let parent_window =
                foreground::wait_window_of_pid(parent, 5000).ok_or("parent window not found")?;
            if !foreground::bring_to_foreground(root)? {
                return Err("target focus failed".into());
            }
            let captured = uia.focused()?;
            let baseline = uia.read_back(&captured)?;
            let mut variants = vec![];
            for use_uia in [false, true] {
                if !foreground::bring_to_foreground(parent_window)? {
                    return Err("parent focus failed".into());
                }
                let activated =
                    unsafe { windows::Win32::UI::WindowsAndMessaging::SetForegroundWindow(root) }
                        .as_bool();
                let after_activate = foreground::foreground();
                let same_before = uia
                    .focused()
                    .is_ok_and(|now| uia.same_element(&captured, &now));
                let focus_result = if use_uia {
                    Some(unsafe { captured.SetFocus() }.map_err(|e| e.to_string()))
                } else {
                    None
                };
                let after_focus = foreground::foreground();
                let same_after = uia
                    .focused()
                    .is_ok_and(|now| uia.same_element(&captured, &now));
                variants.push(json!({"use_uia_set_focus":use_uia,"activated":activated,
                    "after_activate":after_activate,"same_element_before_set_focus":same_before,
                    "focus_result":focus_result,"after_focus":after_focus,"same_element_after_set_focus":same_after,
                    "readback":uia.read_back(&captured)}));
            }
            Ok(
                json!({"root_hwnd":root.0 as isize,"baseline":baseline,"variants":variants,
                "scope":"independent Win32 activation vs UIA SetFocus control; production unchanged"}),
            )
        }
        "probeReadNow" => {
            let pid = fixture_pid(request)?;
            let el = uia.focused()?;
            let info = uia.describe(&el);
            if info.pid != pid as i32 || foreground::foreground().is_none_or(|f| f.pid != pid) {
                return Err("not the fixture".into());
            }
            if info.is_password {
                return Err("protected fixture".into());
            }
            use windows::Win32::UI::Accessibility::{
                IUIAutomationTextPattern, IUIAutomationTextPattern2, UIA_TextPattern2Id,
                UIA_TextPatternId,
            };
            let pattern =
                unsafe { el.GetCurrentPatternAs::<IUIAutomationTextPattern>(UIA_TextPatternId) };
            let selection = pattern.as_ref().ok().map(|tp| unsafe {
                tp.GetSelection()
                    .and_then(|s| s.Length())
                    .map_err(|e| e.to_string())
            });
            let document = pattern.as_ref().ok().map(|tp| unsafe {
                tp.DocumentRange()
                    .and_then(|d| d.GetText(200001))
                    .map(|s| s.to_string())
                    .map_err(|e| e.to_string())
            });
            let embedded_children = pattern.as_ref().ok().map(|tp| unsafe {
                tp.DocumentRange()
                    .and_then(|d| d.GetChildren())
                    .and_then(|c| c.Length())
                    .map_err(|e| e.to_string())
            });
            let value = unsafe {
                el.GetCurrentPatternAs::<windows::Win32::UI::Accessibility::IUIAutomationValuePattern>(
                    windows::Win32::UI::Accessibility::UIA_ValuePatternId)
                    .and_then(|v| v.CurrentValue()).map(|v| v.to_string()).map_err(|e| e.to_string())
            };
            let pattern2 =
                unsafe { el.GetCurrentPatternAs::<IUIAutomationTextPattern2>(UIA_TextPattern2Id) };
            let mut active = windows::core::BOOL(0);
            let caret = pattern2.as_ref().ok().map(|tp| unsafe {
                tp.GetCaretRange(&mut active)
                    .and_then(|r| r.GetText(20))
                    .map(|s| s.to_string())
                    .map_err(|e| e.to_string())
            });
            Ok(
                json!({"info":info,"readback":uia.read_back(&el),"selection_count":selection,"document":document,
                "value":value,"embedded_children":embedded_children,
                "caret_pattern2":caret,"caret_active":active.as_bool()}),
            )
        }
        "capture" => {
            let pid = fixture_pid(request)?;
            if foreground::foreground().is_none_or(|f| f.pid != pid) {
                return Err("foreground is not fixture".into());
            }
            let capture = uia.readable_with_retry(1500)?;
            let el = capture.element;
            let info = uia.describe(&el);
            if info.pid != pid as i32 {
                return Err("focused element is not fixture".into());
            }
            let class = crate::uia::classify(&info);
            Ok(
                json!({"info":info,"class":class,"attempts":capture.attempts.len(),"attempt_details":capture.attempts,"readback":capture.readback,
                "caret":uia.caret_chain(Some(&el)),"foreground":foreground::foreground()}),
            )
        }
        "inspectPoint" => {
            let pid = fixture_pid(request)?;
            let x = request["x"]
                .as_i64()
                .and_then(|v| i32::try_from(v).ok())
                .ok_or("x required")?;
            let y = request["y"]
                .as_i64()
                .and_then(|v| i32::try_from(v).ok())
                .ok_or("y required")?;
            let el = uia.at_point(x, y)?;
            let info = uia.describe(&el);
            if info.pid != pid as i32 {
                return Err("point is not in fixture".into());
            }
            Ok(json!({"class":crate::uia::classify(&info),"info":info}))
        }
        "probeFocus" => {
            let pid = fixture_pid(request)?;
            let hwnd =
                foreground::wait_window_of_pid(pid, 5000).ok_or("fixture window not found")?;
            Ok(
                json!({"focused":foreground::bring_to_foreground(hwnd)?,"hwnd":hwnd.0 as isize,
                "foreground":foreground::foreground()}),
            )
        }
        "probeSelect" => {
            let pid = fixture_pid(request)?;
            if foreground::foreground().is_none_or(|f| f.pid != pid) {
                return Err("foreground is not fixture".into());
            }
            let start = request["start"]
                .as_u64()
                .filter(|n| *n <= 100)
                .ok_or("start 0..100 required")?;
            let len = request["length"]
                .as_u64()
                .filter(|n| *n <= 100)
                .ok_or("length 0..100 required")?;
            keys::chord(&[VK_CONTROL, VK_HOME]);
            for _ in 0..start {
                keys::tap(VK_RIGHT);
            }
            if len > 0 {
                keys::down(VK_SHIFT);
                for _ in 0..len {
                    keys::tap(VK_RIGHT);
                }
                keys::up(VK_SHIFT);
            }
            Ok(json!({"dispatched":true,"effect":"none"}))
        }
        "probePaste" => paste::current(
            uia,
            fixture_pid(request)?,
            request["text"].as_str().ok_or("text required")?,
        ),
        "probeHoldModifiers" => {
            let pid = fixture_pid(request)?;
            if foreground::foreground().is_none_or(|f| f.pid != pid) {
                return Err("foreground is not fixture".into());
            }
            keys::down(windows::Win32::UI::Input::KeyboardAndMouse::VK_LCONTROL);
            keys::down(windows::Win32::UI::Input::KeyboardAndMouse::VK_LSHIFT);
            let end = std::time::Instant::now() + std::time::Duration::from_millis(200);
            while std::time::Instant::now() < end {
                let held = keys::held_modifiers();
                if held.contains(&0xA2) && held.contains(&0xA0) {
                    break;
                }
                std::thread::sleep(std::time::Duration::from_millis(5));
            }
            Ok(json!({"held":keys::held_modifiers()}))
        }
        "probeReleaseModifiers" => {
            Ok(json!({"released":keys::release_held_modifiers(),"held":keys::held_modifiers()}))
        }
        "probeSeedClipboard" => crate::probe_clipboard::seed(request),
        "probeClipboardState" => crate::probe_clipboard::state(),
        "probeClipboardCycle" => {
            use crate::clipboard as clip;
            if std::env::var("GITHUB_ACTIONS").as_deref() != Ok("true") {
                return Err("clipboard cycle is restricted to the disposable CI fixture".into());
            }
            let entry = |text: &str| clip::Entry {
                format: clip::CF_UNICODE,
                data: crate::wide(text)
                    .iter()
                    .flat_map(|unit| unit.to_le_bytes())
                    .collect(),
            };
            clip::write_entries(&[entry("SVI original clipboard")])?;
            let original = clip::snapshot()?;
            let write = clip::set_text_excluded("SVI publication fixture", original.seq)
                .map_err(|e| e.message)?;
            let current_seq = clip::sequence();
            let restored = clip::restore_if_unchanged(&original, write.seq)?;
            let restored_text = clip::read_unicode_text()?;
            let baseline = clip::snapshot()?;
            let second = clip::set_text_excluded("SVI second publication", baseline.seq)
                .map_err(|e| e.message)?;
            // Same API, new version: even a change under our own window must not be overwritten.
            clip::write_entries(&[entry("SVI intervening clipboard")])?;
            let replaced_seq = clip::sequence();
            let replaced_restored = clip::restore_if_unchanged(&baseline, second.seq)?;
            let replaced_text = clip::read_unicode_text()?;
            Ok(
                json!({"write":write,"current_seq":current_seq,"restored":restored,"restored_text":restored_text,
                "second":second,"replaced_seq":replaced_seq,"replaced_restored":replaced_restored,"replaced_text":replaced_text}),
            )
        }
        "probeHotkeys" => Ok(
            json!({"probes":hotkey::standard_combos().iter().map(|c| hotkey::probe(c, false)).collect::<Vec<_>>()}),
        ),
        "describeWindow" => {
            let pid = fixture_pid(request)?;
            let handle =
                foreground::wait_window_of_pid(pid, 5000).ok_or("fixture window not found")?;
            Ok(
                json!({"info":foreground::describe(HWND(handle.0)),"integrity":integrity::compare_with_pid(pid)}),
            )
        }
        _ => {
            Err("unknown technical-probe action; product insert actions are not implemented".into())
        }
    }
}
