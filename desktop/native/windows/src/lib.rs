//! SmartVoiceInput Windows input helper (v0.0.2) and its technical-verification building blocks.
//!
//! Every module here is a thin, individually testable wrapper over one Win32 concern
//! (see docs/02-prd/windows-version-decision-20261029.md, section 5 and 6).
#[cfg(windows)]
pub mod clipboard;
pub mod dib_policy;
#[cfg(windows)]
pub mod foreground;
#[cfg(windows)]
pub mod hotkey;
pub mod input_policy;
#[cfg(windows)]
pub mod integrity;
#[cfg(windows)]
pub mod keys;
#[cfg(windows)]
pub mod launch;
pub mod modifier_policy;
#[cfg(windows)]
pub mod modifier_watch;
#[cfg(windows)]
pub mod paste;
#[cfg(windows)]
pub mod probe_clipboard;
#[cfg(windows)]
pub mod probe_protocol;
#[cfg(windows)]
pub mod product;
pub mod report;
#[cfg(windows)]
pub mod target_lifetime;
#[cfg(windows)]
pub mod uia;
pub mod verification;

/// Errors are plain strings: the helper reports them as JSON, never panics across the protocol.
pub type R<T> = Result<T, String>;

/// Map any displayable error to a contextual `String`.
pub fn ctx<E: std::fmt::Display>(what: &'static str) -> impl Fn(E) -> String {
    move |e| format!("{what}: {e}")
}

/// UTF-16 with a trailing NUL, for APIs taking PCWSTR.
pub fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// Milliseconds since an arbitrary monotonic point (for timing fields).
pub fn now_ms() -> f64 {
    use std::time::Instant;
    static START: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_secs_f64() * 1000.0
}
