//! Independent feasibility probe. This is deliberately not wired to the production insert actions.
//! Only a fixture PID explicitly supplied by the harness may receive this one paste.
use crate::{
    clipboard, foreground, keys,
    uia::{TargetClass, Uia},
    verification, R,
};
use serde_json::{json, Value};
use std::time::{Duration, Instant};

pub fn current(uia: &Uia, fixture_pid: u32, text: &str) -> R<Value> {
    if text.is_empty() {
        return Err("empty paste text is not a valid probe".into());
    }
    if std::env::var("SVI_WINDOWS_PROBE").as_deref() != Ok("1") || fixture_pid == 0 {
        return Err("probePaste requires SVI_WINDOWS_PROBE=1 and an explicit fixture PID".into());
    }
    let started = Instant::now();
    let admission = foreground::admit_foreground();
    if !admission.allowed {
        return Ok(
            json!({"status":"rejected","effect":"none","observable":false,"admission":admission}),
        );
    }
    let identity = admission
        .info
        .as_ref()
        .ok_or("missing foreground identity")?;
    if identity.pid != fixture_pid {
        return Err("foreground is not the requested fixture PID".into());
    }
    let (element, attempts) = uia.focused_with_retry(1200)?;
    let info = uia.describe(&element);
    if info.pid != fixture_pid as i32 || !info.has_keyboard_focus {
        return Ok(json!({"status":"rejected","effect":"none","reason":"focus_not_fixture"}));
    }
    let target_class = crate::uia::classify(&info);
    if matches!(
        target_class,
        TargetClass::Password | TargetClass::ReadOnly | TargetClass::Disabled
    ) {
        return Ok(json!({"status":"rejected","effect":"none","reason":target_class}));
    }
    let before = uia.read_back(&element).ok();
    let expected = before
        .as_ref()
        .and_then(|b| verification::expected_after(b, text));
    let original = clipboard::snapshot()?;
    let unsafe_formats: Vec<_> = original
        .skipped
        .iter()
        .filter(|s| s.reason != "synthesised_from_unicode_text")
        .collect();
    if !unsafe_formats.is_empty() {
        return Ok(
            json!({"status":"rejected","effect":"none","reason":"clipboard_snapshot_incomplete","skipped":unsafe_formats}),
        );
    }
    let released = keys::wait_modifiers_released(500);
    if !released && crate::modifier_watch::has_unknown_held() {
        return Ok(
            json!({"status":"rejected","effect":"none","reason":"modifier_provenance_unknown"}),
        );
    }
    let neutralised = if released {
        vec![]
    } else {
        keys::release_held_modifiers()
    };
    let publication = match clipboard::set_text_excluded(text, original.seq) {
        Ok(publication) => publication,
        Err(error) => {
            // EmptyClipboard may already have happened. Make restoration best effort and report it.
            let restored = error
                .owned_seq
                .map(|seq| clipboard::restore_if_unchanged(&original, seq));
            let _ = crate::modifier_watch::restore_still_physically_held(&neutralised);
            return Ok(
                json!({"status":"rejected","effect":"none","reason":error.message,"clipboard_restore":format!("{restored:?}")}),
            );
        }
    };
    let seq = publication.seq;
    let same_focus = uia.focused().is_ok_and(|e| uia.same_element(&element, &e));
    let fresh = foreground::admit_foreground();
    let same_target = fresh.allowed
        && fresh.info.as_ref().is_some_and(|f| {
            f.hwnd == identity.hwnd
                && f.pid == fixture_pid
                && f.process_birth_100ns == identity.process_birth_100ns
        });
    let same_clipboard = clipboard::still_owned(seq).unwrap_or(false);
    if !same_target || !same_focus || !same_clipboard {
        let restored = clipboard::restore_if_unchanged(&original, seq);
        let _ = crate::modifier_watch::restore_still_physically_held(&neutralised);
        return Ok(
            json!({"status":"rejected","effect":"none","reason":if same_clipboard {"target_changed"} else {"clipboard_changed"},"clipboard_restore":format!("{restored:?}")}),
        );
    }
    // No UIA SetValue, Enter, focus restoration, or automatic retry.
    let accepted = keys::send_ctrl_v();
    let restored_modifiers = crate::modifier_watch::restore_still_physically_held(&neutralised);
    std::thread::sleep(Duration::from_millis(350));
    let until = Instant::now() + Duration::from_millis(1000);
    let mut after = None;
    let mut matched = false;
    loop {
        if Instant::now() > until {
            matched = false;
            break;
        }
        if let (Some(fg), Ok(el)) = (foreground::foreground(), uia.focused()) {
            if fg.hwnd == identity.hwnd
                && fg.process_birth_100ns == identity.process_birth_100ns
                && uia.same_element(&element, &el)
            {
                after = uia.read_back(&el).ok();
                matched = verification::exactly_matches(expected.as_ref(), after.as_ref());
            }
        }
        if Instant::now() > until {
            matched = false;
            break;
        }
        if matched {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let before_restore_seq = clipboard::sequence();
    let restored = clipboard::restore_if_unchanged(&original, seq);
    Ok(json!({
        "status":if matched { "inserted" } else { "unverified" },
        "effect":if matched { "written" } else { "unknown" },
        "observable":matched,"send_input_accepted":accepted,"uia_attempts":attempts,
        "before":before,"after":after,"expected":expected,"neutralised_modifiers":neutralised,
        "held_modifiers_after":keys::held_modifiers(),"physical_modifier_restore_verified":false,
        "restored_physically_held_modifiers":restored_modifiers,
        "clipboard_restored":restored.as_ref().ok(),"clipboard_restore_error":restored.err(),
        "clipboard_publication":publication,"clipboard_seq_before_restore":before_restore_seq,
        "elapsed_ms":started.elapsed().as_secs_f64()*1000.0
    }))
}
