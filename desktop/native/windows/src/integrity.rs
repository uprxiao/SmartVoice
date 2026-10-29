//! Integrity-level comparison (UIPI). `SendInput` into a higher-integrity window is dropped
//! silently (no error, no GetLastError), so the caller must compare levels itself.
//! `TokenElevation` is deliberately not used: it cannot tell High from System.
use crate::{ctx, R};
use serde::Serialize;
use windows::Win32::Foundation::{CloseHandle, HANDLE};
use windows::Win32::Security::{
    GetSidSubAuthority, GetSidSubAuthorityCount, GetTokenInformation, TokenIntegrityLevel,
    TOKEN_MANDATORY_LABEL, TOKEN_QUERY,
};
use windows::Win32::System::Threading::{
    GetCurrentProcess, OpenProcess, OpenProcessToken, PROCESS_QUERY_LIMITED_INFORMATION,
};

pub const RID_LOW: u32 = 0x1000;
pub const RID_MEDIUM: u32 = 0x2000;
pub const RID_HIGH: u32 = 0x3000;
pub const RID_SYSTEM: u32 = 0x4000;

pub fn level_name(rid: u32) -> &'static str {
    match rid {
        0..=0x0fff => "untrusted",
        0x1000..=0x1fff => "low",
        0x2000..=0x2fff => "medium",
        0x3000..=0x3fff => "high",
        _ => "system",
    }
}

/// Relation of a target process to this process, from the injector's point of view.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Relation {
    /// Same level: injection allowed.
    Same,
    /// Target is lower: injection allowed.
    TargetLower,
    /// Target is higher: UIPI will drop the input.
    TargetHigher,
    /// Could not open the target at all (ERROR_ACCESS_DENIED): treat as higher.
    TargetDenied,
}

impl Relation {
    pub fn injection_allowed(self) -> bool {
        matches!(self, Relation::Same | Relation::TargetLower)
    }
}

/// RID of an open token (e.g. 0x2000 for Medium).
pub fn rid_of_token(token: HANDLE) -> R<u32> {
    unsafe {
        let mut len = 0u32;
        // First call only reports the required size; it is expected to fail.
        let _ = GetTokenInformation(token, TokenIntegrityLevel, None, 0, &mut len);
        if len == 0 {
            return Err("GetTokenInformation(size) returned 0".into());
        }
        let mut buf = vec![0u8; len as usize];
        GetTokenInformation(
            token,
            TokenIntegrityLevel,
            Some(buf.as_mut_ptr() as *mut _),
            len,
            &mut len,
        )
        .map_err(ctx("GetTokenInformation(TokenIntegrityLevel)"))?;
        let label = &*(buf.as_ptr() as *const TOKEN_MANDATORY_LABEL);
        let sid = label.Label.Sid;
        let count = *GetSidSubAuthorityCount(sid) as u32;
        if count == 0 {
            return Err("SID without sub-authorities".into());
        }
        Ok(*GetSidSubAuthority(sid, count - 1))
    }
}

pub fn own_rid() -> R<u32> {
    unsafe {
        let mut token = HANDLE::default();
        OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token)
            .map_err(ctx("OpenProcessToken(self)"))?;
        let rid = rid_of_token(token);
        let _ = CloseHandle(token);
        rid
    }
}

/// `Err(true)` = access denied (target is above us), `Err(false)` = other failure.
pub fn process_rid(pid: u32) -> Result<u32, bool> {
    unsafe {
        let process = match OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) {
            Ok(h) => h,
            Err(e) => return Err(e.code().0 as u32 == 0x8007_0005),
        };
        let mut token = HANDLE::default();
        let opened = OpenProcessToken(process, TOKEN_QUERY, &mut token);
        let _ = CloseHandle(process);
        if let Err(e) = opened {
            return Err(e.code().0 as u32 == 0x8007_0005);
        }
        let rid = rid_of_token(token);
        let _ = CloseHandle(token);
        rid.map_err(|_| false)
    }
}

pub fn compare_with_pid(pid: u32) -> (Relation, Option<u32>) {
    let Ok(own) = own_rid() else {
        return (Relation::TargetDenied, None);
    };
    match process_rid(pid) {
        Ok(rid) => {
            let rel = if rid > own {
                Relation::TargetHigher
            } else if rid == own {
                Relation::Same
            } else {
                Relation::TargetLower
            };
            (rel, Some(rid))
        }
        Err(true) => (Relation::TargetDenied, None),
        // Unknown for another reason: be conservative like "denied".
        Err(false) => (Relation::TargetDenied, None),
    }
}

pub fn process_birth(pid: u32) -> Option<u64> {
    use windows::Win32::Foundation::FILETIME;
    use windows::Win32::System::Threading::GetProcessTimes;
    unsafe {
        let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid).ok()?;
        let (mut birth, mut exit, mut kernel, mut user) = (
            FILETIME::default(),
            FILETIME::default(),
            FILETIME::default(),
            FILETIME::default(),
        );
        let result = GetProcessTimes(process, &mut birth, &mut exit, &mut kernel, &mut user);
        let _ = CloseHandle(process);
        result.ok()?;
        Some(((birth.dwHighDateTime as u64) << 32) | birth.dwLowDateTime as u64)
    }
}
