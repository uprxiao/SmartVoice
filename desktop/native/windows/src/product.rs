//! Production actions. No fixture PID, activation aid, probe action or content logging.
use crate::{
    clipboard, foreground,
    input_policy::{Effect, Executions, Request},
    keys, modifier_watch,
    uia::{classify, TargetClass, Uia},
    verification::{self, Readback},
};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    time::{Duration, Instant},
};
use windows::Win32::{
    Foundation::HWND,
    UI::{
        Accessibility::IUIAutomationElement,
        WindowsAndMessaging::{IsWindow, SetForegroundWindow},
    },
};

struct Target {
    window: foreground::ForegroundInfo,
    element: Option<IUIAutomationElement>,
    before: Option<Readback>,
}
pub struct Product {
    pub uia: Uia,
    parent: u32,
    targets: BTreeMap<u64, Target>,
    sequence: u64,
    executions: Executions,
}
fn failed(code: &str, message: &str) -> Value {
    json!({"status":"failed","effect":"none","observable":false,"code":code,"message":message})
}
fn unverified() -> Value {
    json!({"status":"unverified","effect":"unknown","observable":false,"code":"VERIFY_FAILED",
        "message":"无法确认是否已输入，请先检查目标；不会自动重试"})
}
fn readable(uia: &Uia, element: &IUIAutomationElement) -> Option<Readback> {
    let info = uia.describe(element);
    if matches!(
        classify(&info),
        TargetClass::Password | TargetClass::ReadOnly | TargetClass::Disabled
    ) {
        return None;
    }
    uia.read_back(element)
        .ok()
        .filter(|r| r.text.encode_utf16().count() <= 200000)
}
fn protected(uia: &Uia, element: &IUIAutomationElement) -> bool {
    let info = uia.describe(element);
    matches!(
        classify(&info),
        TargetClass::Password | TargetClass::ReadOnly | TargetClass::Disabled
    ) || matches!(
        info.control_type,
        50000
            | 50001
            | 50002
            | 50005
            | 50006
            | 50007
            | 50008
            | 50009
            | 50010
            | 50011
            | 50012
            | 50013
            | 50014
            | 50015
            | 50016
            | 50017
            | 50018
            | 50019
            | 50021
            | 50022
            | 50023
            | 50024
            | 50034
            | 50035
    )
}
fn same_window(a: &foreground::ForegroundInfo, b: &foreground::ForegroundInfo) -> bool {
    a.hwnd == b.hwnd
        && a.pid == b.pid
        && a.process_birth_100ns.is_some()
        && a.process_birth_100ns == b.process_birth_100ns
}
fn baseline_equal(a: Option<&Readback>, b: Option<&Readback>) -> bool {
    matches!((a,b),(Some(a),Some(b)) if a.caret_utf16.is_some() && a.selection_utf16_len.is_some()
        && a.text==b.text && a.caret_utf16==b.caret_utf16 && a.selection_utf16_len==b.selection_utf16_len)
}
// Scope guards restore only our clipboard publication and still-physical modifier keys.
struct Modifiers(Vec<u16>);
impl Drop for Modifiers {
    fn drop(&mut self) {
        let _ = modifier_watch::restore_still_physically_held(&self.0);
    }
}
struct Clip<'a> {
    snapshot: &'a clipboard::Snapshot,
    seq: u32,
}
impl Drop for Clip<'_> {
    fn drop(&mut self) {
        let _ = clipboard::restore_if_unchanged(self.snapshot, self.seq);
    }
}

impl Product {
    pub fn new(uia: Uia, parent: u32) -> Self {
        Self {
            uia,
            parent,
            targets: BTreeMap::new(),
            sequence: 0,
            executions: Executions::default(),
        }
    }
    fn current(&self, warmup: bool) -> Result<Target, Value> {
        let admission = foreground::admit_foreground();
        if !admission.allowed {
            return Err(match admission.reject {
                Some(foreground::Reject::TargetHigherIntegrity) => failed(
                    "PERMISSION_DENIED",
                    "目标窗口以管理员运行，无法自动输入。请复制结果后手动粘贴。",
                ),
                _ => failed("TARGET_UNSUPPORTED", "当前窗口无法安全输入，结果已保留"),
            });
        }
        let window = admission
            .info
            .ok_or_else(|| failed("TARGET_CHANGED", "当前窗口不可确认"))?;
        if window.pid == self.parent {
            return Err(failed("TARGET_UNSUPPORTED", "请先切换到需要输入的应用"));
        }
        let mut element = self.uia.focused().ok();
        // Warm up lazy Chromium once, but a genuinely empty input with a caret is already usable.
        if warmup {
            let end = Instant::now() + Duration::from_millis(450);
            while element.as_ref().is_none_or(|el| {
                let info = self.uia.describe(el);
                !protected(&self.uia, el)
                    && readable(&self.uia, el).is_none_or(|r| r.caret_utf16.is_none())
                    && info.has_keyboard_focus
            }) && Instant::now() < end
            {
                std::thread::sleep(Duration::from_millis(75));
                element = self.uia.focused().ok();
            }
        }
        if let Some(el) = element.as_ref() {
            let info = self.uia.describe(el);
            if protected(&self.uia, el) {
                return Err(failed(
                    "TARGET_UNSUPPORTED",
                    "当前输入框受保护、只读或已禁用，结果已保留",
                ));
            }
            if info.pid != window.pid as i32 || !info.has_keyboard_focus {
                element = None;
            }
        }
        let before = element.as_ref().and_then(|el| readable(&self.uia, el));
        if !foreground::foreground().is_some_and(|f| same_window(&window, &f)) {
            return Err(failed("TARGET_CHANGED", "写入前目标已变化，本次没有输入"));
        }
        Ok(Target {
            window,
            element,
            before,
        })
    }
    fn keep(&mut self, target: Target) -> String {
        while self.targets.len() >= 64 {
            self.targets.pop_first();
        }
        self.sequence += 1;
        let id = self.sequence;
        self.targets.insert(id, target);
        format!("win-{id}")
    }
    fn key(id: &str) -> Option<u64> {
        id.strip_prefix("win-")?.parse().ok()
    }
    fn validate(&self, id: &str, restore: bool) -> bool {
        let Some(target) = Self::key(id).and_then(|k| self.targets.get(&k)) else {
            return false;
        };
        if target.before.is_none() || target.element.is_none() {
            return false;
        }
        let hwnd = HWND(target.window.hwnd as *mut _);
        if !unsafe { IsWindow(Some(hwnd)) }.as_bool()
            || !same_window(&target.window, &foreground::describe(hwnd))
        {
            return false;
        }
        if restore && foreground::input_desktop_accessible() {
            let Some(current) = foreground::foreground() else {
                return false;
            };
            if current.pid != self.parent && !same_window(&target.window, &current) {
                return false;
            }
            if !same_window(&target.window, &current) {
                unsafe {
                    let _ = SetForegroundWindow(hwnd);
                }
                if let Some(el) = &target.element {
                    let _ = unsafe { el.SetFocus() };
                }
            }
        }
        let Ok(current) = self.current(false) else {
            return false;
        };
        same_window(&target.window, &current.window)
            && matches!((&target.element,&current.element),(Some(a),Some(b)) if self.uia.same_element(a,b))
            && baseline_equal(target.before.as_ref(), current.before.as_ref())
    }
    pub fn dispatch(&mut self, request: &Request) -> Value {
        match request.action.as_str() {
            "permission" => json!({"supported":true,"trusted":true,"observationSupported":false}),
            "capture" => match self.current(true) {
                Ok(t) => {
                    let observable = t.before.as_ref().is_some_and(|b| {
                        b.caret_utf16.is_some() && b.selection_utf16_len.is_some()
                    });
                    let app = format!("PID {}", t.window.pid);
                    let id = self.keep(t);
                    json!({"target":id,"app":app,"observable":observable})
                }
                Err(e) => {
                    json!({"error":{"code":e["code"],"message":e["message"],"effect":"none"}})
                }
            },
            "release" => {
                if let Some(k) = request.target.as_deref().and_then(Self::key) {
                    self.targets.remove(&k);
                }
                json!({"released":true})
            }
            "validate" => {
                let valid = self.validate(
                    request.target.as_deref().unwrap_or(""),
                    request.restore_focus,
                );
                json!({"valid":valid,"code":if valid {""} else {"TARGET_CHANGED"},"message":if valid {""} else {"指定的输入位置已变化，请重新指定或复制结果"}})
            }
            "inputBounds" => {
                let el = self.uia.focused().ok();
                let fix = self.uia.caret_chain(el.as_ref());
                match fix.rect {
                    Some(r) => {
                        json!({"available":true,"coordinateSpace":"physical","source":fix.chosen,
                    "bounds":{"x":r[0],"y":r[1],"width":(r[2]-r[0]).max(1),"height":(r[3]-r[1]).max(1)}})
                    }
                    None => json!({"available":false}),
                }
            }
            "insert" | "insertCurrent" => {
                let id = request.insertion_id.as_deref().unwrap_or("");
                if let Err(effect) = self.executions.begin(id) {
                    return if effect == Effect::None {
                        failed("ZERO_WRITE", "本次输入已登记或执行容量已满，请重新开始")
                    } else {
                        unverified()
                    };
                }
                let response = self.insert(request);
                self.executions.finish(
                    id,
                    if response["effect"] == "none" {
                        Effect::None
                    } else {
                        Effect::Unknown
                    },
                );
                response
            }
            _ => {
                json!({"error":{"code":"NATIVE_REQUEST_INVALID","message":"不支持的输入动作","effect":"none"}})
            }
        }
    }
    fn insert(&mut self, r: &Request) -> Value {
        let released = keys::wait_modifiers_released(500);
        if !released && modifier_watch::has_unknown_held() {
            return failed("ZERO_WRITE", "快捷键状态尚未确认，请松开按键后重试");
        }
        let modifiers = Modifiers(if released {
            vec![]
        } else {
            keys::release_held_modifiers()
        });
        if !keys::held_modifiers().is_empty() {
            return failed("ZERO_WRITE", "请松开快捷键后重试");
        }
        let explicit = r.action == "insert";
        if explicit && !self.validate(r.target.as_deref().unwrap_or(""), r.restore_focus) {
            return failed(
                "TARGET_CHANGED",
                "指定的输入位置已变化，请重新指定或复制结果",
            );
        }
        if let Err(e) = self.current(true) {
            return e;
        }
        let snapshot = match clipboard::snapshot() {
            Ok(s) => s,
            Err(_) => {
                return failed(
                    "TARGET_UNSUPPORTED",
                    "无法安全保存剪贴板，请复制结果后手动粘贴",
                )
            }
        };
        if snapshot
            .skipped
            .iter()
            .any(|s| s.reason != "synthesised_from_unicode_text")
        {
            return failed(
                "TARGET_UNSUPPORTED",
                "剪贴板格式无法安全保留，请复制结果后手动粘贴",
            );
        }
        let publication =
            match clipboard::set_text_excluded(r.text.as_deref().unwrap_or(""), snapshot.seq) {
                Ok(p) => p,
                Err(e) => {
                    if let Some(seq) = e.owned_seq {
                        let _ = clipboard::restore_if_unchanged(&snapshot, seq);
                    }
                    return failed("ZERO_WRITE", "剪贴板已变化或不可用，本次没有输入");
                }
            };
        let clip = Clip {
            snapshot: &snapshot,
            seq: publication.seq,
        };
        // Obtain the latest position after both the modifier wait and clipboard publication.
        if explicit && !self.validate(r.target.as_deref().unwrap_or(""), false) {
            return failed("TARGET_CHANGED", "指定的输入位置已变化，本次没有输入");
        }
        let target = match self.current(false) {
            Ok(t) => t,
            Err(e) => return e,
        };
        if !clipboard::still_owned(clip.seq).unwrap_or(false) || !keys::held_modifiers().is_empty()
        {
            return failed("ZERO_WRITE", "写入前剪贴板或按键已变化，本次没有输入");
        }
        let fresh = foreground::admit_foreground();
        if !fresh.allowed
            || !fresh.info.is_some_and(|f| same_window(&target.window, &f))
            || target.element.as_ref().is_some_and(|el| {
                protected(&self.uia, el)
                    || !self
                        .uia
                        .focused()
                        .is_ok_and(|now| self.uia.same_element(el, &now))
            })
        {
            return failed("TARGET_CHANGED", "写入前目标已变化，本次没有输入");
        }
        let expected = target
            .before
            .as_ref()
            .and_then(|b| verification::expected_after(b, r.text.as_deref().unwrap_or("")));
        // Exactly one attempt; accepted input count is deliberately not used as delivery evidence.
        let _ = keys::send_ctrl_v();
        drop(modifiers);
        std::thread::sleep(Duration::from_millis(350));
        let end = Instant::now() + Duration::from_millis(1000);
        let matched = loop {
            let matched = foreground::foreground().is_some_and(|f| same_window(&target.window, &f))
                && target.element.as_ref().is_some_and(|el| {
                    self.uia
                        .focused()
                        .is_ok_and(|now| self.uia.same_element(el, &now))
                        && verification::exactly_matches(
                            expected.as_ref(),
                            readable(&self.uia, el).as_ref(),
                        )
                });
            if matched || Instant::now() >= end {
                break matched;
            }
            std::thread::sleep(Duration::from_millis(75));
        };
        let restored = clipboard::restore_if_unchanged(&snapshot, clip.seq);
        // Guard can safely run again: the restored/new version no longer matches this publication.
        drop(clip);
        let mut response = if matched {
            json!({"status":"inserted","effect":"written","observable":true})
        } else {
            unverified()
        };
        response["clipboardRestored"] = json!(restored.as_ref().is_ok_and(|r| *r));
        if restored.is_err() {
            response["message"] = json!("剪贴板恢复失败，请检查剪贴板；本次不会再次输入");
        }
        if matched {
            let app = format!("PID {}", target.window.pid);
            let id = self.keep(target);
            response["target"] = id.into();
            response["app"] = app.into();
        }
        response
    }
}
