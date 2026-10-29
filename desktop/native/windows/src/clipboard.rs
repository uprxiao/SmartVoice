//! Clipboard snapshot / write-with-exclusions / conditional restore.
//!
//! Text is written with the three registered history/cloud exclusion formats. Actual Win+V/cloud
//! behavior still requires V-05 on real Windows. Windows decides eligibility at write time;
//! clearing the clipboard afterwards would not remove an item already recorded.
use crate::{ctx, wide, R};
use serde::Serialize;
use windows::core::PCWSTR;
use windows::Win32::Foundation::{GlobalFree, HANDLE, HGLOBAL, HWND};
use windows::Win32::Graphics::Gdi::{
    GetDC, GetDIBits, GetObjectW, ReleaseDC, BITMAP, BITMAPINFO, BITMAPINFOHEADER, BI_RGB,
    DIB_RGB_COLORS, HBITMAP, HGDIOBJ,
};
use windows::Win32::System::DataExchange::{
    CloseClipboard, EmptyClipboard, EnumClipboardFormats, GetClipboardData,
    GetClipboardFormatNameW, GetClipboardOwner, GetClipboardSequenceNumber, OpenClipboard,
    RegisterClipboardFormatW, SetClipboardData,
};
use windows::Win32::System::Memory::{
    GlobalAlloc, GlobalLock, GlobalSize, GlobalUnlock, GMEM_MOVEABLE,
};
use windows::Win32::System::Ole::{
    CF_BITMAP, CF_DIB, CF_DIBV5, CF_ENHMETAFILE, CF_LOCALE, CF_METAFILEPICT, CF_OEMTEXT,
    CF_PALETTE, CF_TEXT, CF_UNICODETEXT,
};
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, HWND_MESSAGE, WINDOW_EX_STYLE, WINDOW_STYLE,
};

pub const CF_UNICODE: u32 = CF_UNICODETEXT.0 as u32;

#[derive(Debug, Clone)]
pub struct Entry {
    pub format: u32,
    pub data: Vec<u8>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Skipped {
    pub format: u32,
    pub name: String,
    pub reason: &'static str,
}

#[derive(Debug, Clone)]
pub struct Snapshot {
    pub entries: Vec<Entry>,
    pub skipped: Vec<Skipped>,
    pub seq: u32,
}

/// Description of one format for reports/comparisons (never contains the content itself).
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct FormatInfo {
    pub format: u32,
    pub name: String,
    pub bytes: usize,
    pub fnv64: String,
}

pub fn fnv64(data: &[u8]) -> String {
    let mut h: u64 = 0xcbf29ce484222325;
    for b in data {
        h ^= *b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    format!("{h:016x}")
}

pub fn format_name(format: u32) -> String {
    let builtin = match format {
        1 => "CF_TEXT",
        2 => "CF_BITMAP",
        3 => "CF_METAFILEPICT",
        7 => "CF_OEMTEXT",
        8 => "CF_DIB",
        9 => "CF_PALETTE",
        13 => "CF_UNICODETEXT",
        14 => "CF_ENHMETAFILE",
        15 => "CF_HDROP",
        16 => "CF_LOCALE",
        17 => "CF_DIBV5",
        _ => "",
    };
    if !builtin.is_empty() {
        return builtin.to_string();
    }
    let mut buf = [0u16; 128];
    let n = unsafe { GetClipboardFormatNameW(format, &mut buf) };
    if n > 0 {
        String::from_utf16_lossy(&buf[..n as usize])
    } else {
        format!("0x{format:04x}")
    }
}

// Ownership callbacks must remain responsive while the protocol thread blocks in stdin/UIA.
fn owner() -> R<HWND> {
    static OWNER: std::sync::OnceLock<R<isize>> = std::sync::OnceLock::new();
    OWNER
        .get_or_init(|| {
            let (send, receive) = std::sync::mpsc::sync_channel(1);
            std::thread::spawn(move || unsafe {
                let result = CreateWindowExW(
                    WINDOW_EX_STYLE(0),
                    windows::core::w!("STATIC"),
                    windows::core::w!("SmartVoiceInput clipboard"),
                    WINDOW_STYLE(0),
                    0,
                    0,
                    0,
                    0,
                    Some(HWND_MESSAGE),
                    None,
                    None,
                    None,
                )
                .map_err(ctx("CreateWindowExW(clipboard owner)"));
                let Ok(window) = result else {
                    let _ = send.send(result.map(|w| w.0 as isize));
                    return;
                };
                if send.send(Ok(window.0 as isize)).is_err() {
                    return;
                }
                let mut message = windows::Win32::UI::WindowsAndMessaging::MSG::default();
                while windows::Win32::UI::WindowsAndMessaging::GetMessageW(&mut message, None, 0, 0)
                    .0
                    > 0
                {
                    let _ = windows::Win32::UI::WindowsAndMessaging::TranslateMessage(&message);
                    windows::Win32::UI::WindowsAndMessaging::DispatchMessageW(&message);
                }
            });
            receive
                .recv_timeout(std::time::Duration::from_secs(2))
                .map_err(|_| "clipboard owner initialization timed out".to_string())?
        })
        .clone()
        .map(|value| HWND(value as *mut _))
}

fn open_with_retry() -> R<()> {
    let owner = owner()?;
    let mut last = String::new();
    for _ in 0..30 {
        match unsafe { OpenClipboard(Some(owner)) } {
            Ok(()) => return Ok(()),
            Err(e) => last = e.to_string(),
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    Err(format!("OpenClipboard kept failing: {last}"))
}

pub fn sequence() -> u32 {
    unsafe { GetClipboardSequenceNumber() }
}

fn registered(name: &str) -> u32 {
    let w = wide(name);
    unsafe { RegisterClipboardFormatW(PCWSTR(w.as_ptr())) }
}

fn is_handle_format(f: u32) -> bool {
    f == CF_BITMAP.0 as u32
        || f == CF_PALETTE.0 as u32
        || f == CF_METAFILEPICT.0 as u32
        || f == CF_ENHMETAFILE.0 as u32
        || (0x0200..=0x03FF).contains(&f)
}

fn copy_global(h: HANDLE) -> Option<Vec<u8>> {
    unsafe {
        let g = HGLOBAL(h.0);
        let size = GlobalSize(g);
        if size == 0 || size > 32 * 1024 * 1024 {
            return None;
        }
        let p = GlobalLock(g);
        if p.is_null() {
            return None;
        }
        let data = std::slice::from_raw_parts(p as *const u8, size).to_vec();
        let _ = GlobalUnlock(g);
        Some(data)
    }
}

// EnumClipboardFormats lists a supplied format before its automatically converted variants.
// Only a DIB-first, palette-free bitmap with independently equal RGB pixels is reconstructible.
// A bitmap-first clipboard or conflicting explicit bitmap remains a pre-write refusal.
fn reproducible_bitmap(entries: &[Entry]) -> bool {
    let Some(dib) = entries
        .iter()
        .find(|e| [CF_DIB.0 as u32, CF_DIBV5.0 as u32].contains(&e.format))
        .and_then(|e| crate::dib_policy::rgb32(&e.data))
    else {
        return false;
    };
    unsafe {
        let Ok(handle) = GetClipboardData(CF_BITMAP.0 as u32) else {
            return false;
        };
        let mut bitmap = BITMAP::default();
        if GetObjectW(
            HGDIOBJ(handle.0),
            std::mem::size_of::<BITMAP>() as i32,
            Some(&mut bitmap as *mut _ as *mut _),
        ) != std::mem::size_of::<BITMAP>() as i32
            || bitmap.bmWidth != dib.width
            || bitmap.bmHeight != dib.height.unsigned_abs() as i32
            || bitmap.bmPlanes != 1
            || bitmap.bmBitsPixel != 32
        {
            return false;
        }
        let dc = GetDC(None);
        if dc.is_invalid() {
            return false;
        }
        let mut info = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: dib.width,
                biHeight: dib.height,
                biPlanes: 1,
                biBitCount: 32,
                biCompression: BI_RGB.0,
                biSizeImage: dib.pixels.len() as u32,
                ..Default::default()
            },
            ..Default::default()
        };
        let mut actual = vec![0u8; dib.pixels.len()];
        let lines = GetDIBits(
            dc,
            HBITMAP(handle.0),
            0,
            dib.height.unsigned_abs(),
            Some(actual.as_mut_ptr() as *mut _),
            &mut info,
            DIB_RGB_COLORS,
        );
        ReleaseDC(None, dc);
        lines == dib.height.unsigned_abs() as i32 && info.bmiHeader.biWidth == dib.width
            && info.bmiHeader.biHeight == dib.height && info.bmiHeader.biBitCount == 32
            && info.bmiHeader.biCompression == BI_RGB.0
            // BI_RGB32's high byte is reserved. Original DIB/V5 bytes are separately retained exactly.
            && actual.chunks_exact(4).zip(dib.pixels.chunks_exact(4)).all(|(a,b)| a[..3] == b[..3])
    }
}

/// Capture every format we can copy as bytes. Handle-based GDI formats and the text/DIB variants
/// Windows synthesises on its own are intentionally skipped (they come back by themselves).
pub fn snapshot() -> R<Snapshot> {
    open_with_retry()?;
    let seq = sequence();
    let mut formats = vec![];
    let mut f = 0u32;
    loop {
        f = unsafe { EnumClipboardFormats(f) };
        if f == 0 {
            break;
        }
        formats.push(f);
    }
    let has_unicode = formats.contains(&CF_UNICODE);
    let mut entries = vec![];
    let mut skipped = vec![];
    let source_image = formats
        .iter()
        .copied()
        .find(|f| [CF_BITMAP.0 as u32, CF_DIB.0 as u32, CF_DIBV5.0 as u32].contains(f));
    let no_palette = !formats.contains(&(CF_PALETTE.0 as u32));
    for f in formats {
        let name = format_name(f);
        let synthesized_text =
            has_unicode && [CF_TEXT.0 as u32, CF_OEMTEXT.0 as u32, CF_LOCALE.0 as u32].contains(&f);
        if is_handle_format(f) {
            skipped.push(Skipped {
                format: f,
                name,
                reason: "gdi_handle_format",
            });
        } else if synthesized_text {
            skipped.push(Skipped {
                format: f,
                name,
                reason: "synthesised_from_unicode_text",
            });
        } else {
            match unsafe { GetClipboardData(f) } {
                Ok(h) if !h.is_invalid() => match copy_global(h) {
                    Some(data)
                        if entries.iter().map(|e: &Entry| e.data.len()).sum::<usize>()
                            + data.len()
                            <= 32 * 1024 * 1024 =>
                    {
                        entries.push(Entry { format: f, data })
                    }
                    Some(_) => skipped.push(Skipped {
                        format: f,
                        name,
                        reason: "snapshot_too_large",
                    }),
                    None => skipped.push(Skipped {
                        format: f,
                        name,
                        reason: "empty_or_unlockable",
                    }),
                },
                _ => skipped.push(Skipped {
                    format: f,
                    name,
                    reason: "no_data_handle",
                }),
            }
        }
    }
    if no_palette
        && source_image.is_some_and(|f| f != CF_BITMAP.0 as u32)
        && skipped.iter().any(|s| s.format == CF_BITMAP.0 as u32)
        && reproducible_bitmap(&entries)
    {
        for skip in &mut skipped {
            if skip.format == CF_BITMAP.0 as u32 {
                skip.reason = "synthesised_bitmap_from_dib";
            }
        }
    }
    unsafe {
        let _ = CloseClipboard();
    }
    Ok(Snapshot {
        entries,
        skipped,
        seq,
    })
}

fn set_global(format: u32, data: &[u8]) -> R<()> {
    unsafe {
        let g = GlobalAlloc(GMEM_MOVEABLE, data.len().max(1)).map_err(ctx("GlobalAlloc"))?;
        let p = GlobalLock(g);
        if p.is_null() {
            let _ = GlobalFree(Some(g));
            return Err("GlobalLock failed".into());
        }
        std::ptr::copy_nonoverlapping(data.as_ptr(), p as *mut u8, data.len());
        let _ = GlobalUnlock(g);
        match SetClipboardData(format, Some(HANDLE(g.0))) {
            Ok(_) => Ok(()), // ownership moved to the system
            Err(e) => {
                let _ = GlobalFree(Some(g));
                Err(format!("SetClipboardData({}): {e}", format_name(format)))
            }
        }
    }
}

pub struct WriteError {
    pub message: String,
    pub owned_seq: Option<u32>,
}

#[derive(Debug, Serialize)]
pub struct Publication {
    pub seq: u32,
    pub seq_before_close: u32,
}

// A publication is not complete until CloseClipboard. Re-open and establish the
// published version under a lock, only while our owner window still owns it.
// A user's intervening EmptyClipboard assigns a different owner: never adopt its version.
fn published_sequence() -> R<Option<u32>> {
    open_with_retry()?;
    let owner = owner()?;
    let result = unsafe { GetClipboardOwner() }
        .ok()
        .filter(|current| *current == owner)
        .map(|_| sequence());
    unsafe {
        let _ = CloseClipboard();
    }
    Ok(result)
}

pub fn still_owned(seq: u32) -> R<bool> {
    open_with_retry()?;
    let owner = owner()?;
    let result = sequence() == seq && unsafe { GetClipboardOwner() }.ok() == Some(owner);
    unsafe {
        let _ = CloseClipboard();
    }
    Ok(result)
}

/// Compare the original version and write under the same lock. Even a partial failure carries
/// only the version observed while we held the lock, never a later user's clipboard version.
pub fn set_text_excluded(text: &str, expected_seq: u32) -> Result<Publication, WriteError> {
    open_with_retry().map_err(|message| WriteError {
        message,
        owned_seq: None,
    })?;
    let mut mutated = false;
    let result: R<()> = (|| {
        if sequence() != expected_seq {
            return Err("clipboard changed after snapshot".into());
        }
        let exclusions = [
            "ExcludeClipboardContentFromMonitorProcessing",
            "CanIncludeInClipboardHistory",
            "CanUploadToCloudClipboard",
        ]
        .map(registered);
        if exclusions.contains(&0) {
            return Err("could not register clipboard exclusion formats".into());
        }
        unsafe {
            EmptyClipboard().map_err(ctx("EmptyClipboard"))?;
        }
        mutated = true;
        let mut utf16: Vec<u8> = vec![];
        for u in text.encode_utf16().chain(std::iter::once(0)) {
            utf16.extend_from_slice(&u.to_le_bytes());
        }
        set_global(CF_UNICODE, &utf16)?;
        for format in exclusions {
            set_global(format, &0u32.to_le_bytes())?;
        }
        Ok(())
    })();
    let seq_before_close = sequence();
    unsafe {
        let _ = CloseClipboard();
    }
    if !mutated {
        return Err(WriteError {
            message: result
                .err()
                .unwrap_or_else(|| "clipboard was not written".into()),
            owned_seq: None,
        });
    }
    let owned_seq = published_sequence().map_err(|message| WriteError {
        message,
        owned_seq: None,
    })?;
    result.map_err(|message| WriteError { message, owned_seq })?;
    let seq = owned_seq.ok_or_else(|| WriteError {
        message: "clipboard changed before publication confirmed".into(),
        owned_seq: None,
    })?;
    Ok(Publication {
        seq,
        seq_before_close,
    })
}

/// Write the snapshot back (used for restore and for building test fixtures).
pub fn write_entries(entries: &[Entry]) -> R<u32> {
    open_with_retry()?;
    let result: R<()> = (|| {
        unsafe {
            EmptyClipboard().map_err(ctx("EmptyClipboard"))?;
        }
        for e in entries {
            set_global(e.format, &e.data)?;
        }
        Ok(())
    })();
    unsafe {
        let _ = CloseClipboard();
    }
    result?;
    Ok(sequence())
}

/// Compare and restore while holding the clipboard lock: a user copy between the check and
/// EmptyClipboard must never be overwritten. Unsupported original formats are rejected by caller.
pub fn restore_if_unchanged(snapshot: &Snapshot, owned_seq: u32) -> R<bool> {
    open_with_retry()?;
    let result = (|| {
        if sequence() != owned_seq {
            return Ok(false);
        }
        unsafe {
            EmptyClipboard().map_err(ctx("EmptyClipboard(restore)"))?;
        }
        for entry in &snapshot.entries {
            set_global(entry.format, &entry.data)?;
        }
        Ok(true)
    })();
    unsafe {
        let _ = CloseClipboard();
    }
    result
}

pub fn clear() -> R<()> {
    open_with_retry()?;
    let r = unsafe { EmptyClipboard() }.map_err(ctx("EmptyClipboard"));
    unsafe {
        let _ = CloseClipboard();
    }
    r
}

/// Current formats with sizes and content hashes (for fidelity comparisons in reports).
pub fn describe_current() -> R<Vec<FormatInfo>> {
    let snap = snapshot()?;
    Ok(snap
        .entries
        .iter()
        .map(|e| FormatInfo {
            format: e.format,
            name: format_name(e.format),
            bytes: e.data.len(),
            fnv64: fnv64(&e.data),
        })
        .collect())
}

pub fn read_unicode_text() -> R<Option<String>> {
    let snap = snapshot()?;
    Ok(snap
        .entries
        .iter()
        .find(|e| e.format == CF_UNICODE)
        .map(|e| {
            let units: Vec<u16> = e
                .data
                .chunks_exact(2)
                .map(|c| u16::from_le_bytes([c[0], c[1]]))
                .collect();
            let end = units.iter().position(|u| *u == 0).unwrap_or(units.len());
            String::from_utf16_lossy(&units[..end])
        }))
}

pub fn dib_format() -> u32 {
    CF_DIB.0 as u32
}
pub fn dibv5_format() -> u32 {
    CF_DIBV5.0 as u32
}
pub fn registered_format(name: &str) -> u32 {
    registered(name)
}
