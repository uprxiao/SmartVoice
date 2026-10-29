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
        "foreground" => Ok(json!({"admission":foreground::admit_foreground()})),
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
            Ok(json!({"held":keys::held_modifiers()}))
        }
        "probeReleaseModifiers" => {
            Ok(json!({"released":keys::release_held_modifiers(),"held":keys::held_modifiers()}))
        }
        "probeSeedClipboard" => {
            if std::env::var("GITHUB_ACTIONS").as_deref() != Ok("true") {
                return Err(
                    "clipboard seeding is restricted to disposable GitHub runner fixtures".into(),
                );
            }
            let text = crate::wide("SVI original clipboard")
                .iter()
                .flat_map(|c| c.to_le_bytes())
                .collect();
            let seq = crate::clipboard::write_entries(&[crate::clipboard::Entry {
                format: crate::clipboard::CF_UNICODE,
                data: text,
            }])?;
            Ok(json!({"sequence":seq,"text":crate::clipboard::read_unicode_text()?}))
        }
        "probeClipboardState" => Ok(
            json!({"text":crate::clipboard::read_unicode_text()?,"sequence":crate::clipboard::sequence()}),
        ),
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
