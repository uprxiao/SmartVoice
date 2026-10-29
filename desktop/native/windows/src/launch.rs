//! CI-only medium-integrity launcher. The actual RID is checked again inside the child;
//! a successful process-creation call is not proof of an interactive, medium-integrity desktop.
use crate::{ctx, wide, R};
use std::path::Path;
use windows::core::{PCWSTR, PWSTR};
use windows::Win32::Foundation::{CloseHandle, HANDLE, WAIT_OBJECT_0};
use windows::Win32::Security::{
    CreateWellKnownSid, DuplicateTokenEx, GetLengthSid, SecurityImpersonation, SetTokenInformation,
    TokenIntegrityLevel, TokenPrimary, WinMediumLabelSid, PSID, SID_AND_ATTRIBUTES,
    TOKEN_ALL_ACCESS, TOKEN_MANDATORY_LABEL,
};
use windows::Win32::System::Environment::{FreeEnvironmentStringsW, GetEnvironmentStringsW};
use windows::Win32::System::SystemServices::SE_GROUP_INTEGRITY;
use windows::Win32::System::Threading::{
    CreateProcessWithTokenW, GetCurrentProcess, GetExitCodeProcess, OpenProcessToken,
    TerminateProcess, WaitForSingleObject, CREATE_NO_WINDOW, CREATE_PROCESS_LOGON_FLAGS,
    CREATE_UNICODE_ENVIRONMENT, PROCESS_INFORMATION, STARTUPINFOW,
};

pub fn medium(program: &Path, arguments: &[String], timeout_ms: u32) -> R<u32> {
    let executable = wide(&program.to_string_lossy());
    let command = std::iter::once(program.to_string_lossy().into_owned())
        .chain(arguments.iter().cloned())
        .map(|s| crate::report::quote_windows_arg(&s))
        .collect::<Vec<_>>()
        .join(" ");
    let mut command = wide(&command);
    let mut desktop = wide("winsta0\\default");
    unsafe {
        let mut original = HANDLE::default();
        OpenProcessToken(GetCurrentProcess(), TOKEN_ALL_ACCESS, &mut original)
            .map_err(ctx("OpenProcessToken(launcher)"))?;
        let mut token = HANDLE::default();
        let duplicate = DuplicateTokenEx(
            original,
            TOKEN_ALL_ACCESS,
            None,
            SecurityImpersonation,
            TokenPrimary,
            &mut token,
        );
        let _ = CloseHandle(original);
        duplicate.map_err(ctx("DuplicateTokenEx"))?;
        let result = (|| {
            let mut sid = [0u8; 68];
            let mut sid_len = sid.len() as u32;
            CreateWellKnownSid(
                WinMediumLabelSid,
                None,
                Some(PSID(sid.as_mut_ptr() as *mut _)),
                &mut sid_len,
            )
            .map_err(ctx("CreateWellKnownSid(medium)"))?;
            let label = TOKEN_MANDATORY_LABEL {
                Label: SID_AND_ATTRIBUTES {
                    Sid: PSID(sid.as_mut_ptr() as *mut _),
                    Attributes: SE_GROUP_INTEGRITY as u32,
                },
            };
            SetTokenInformation(
                token,
                TokenIntegrityLevel,
                &label as *const _ as *const _,
                std::mem::size_of::<TOKEN_MANDATORY_LABEL>() as u32 + GetLengthSid(label.Label.Sid),
            )
            .map_err(ctx("SetTokenInformation(medium)"))?;
            let startup = STARTUPINFOW {
                cb: std::mem::size_of::<STARTUPINFOW>() as u32,
                lpDesktop: PWSTR(desktop.as_mut_ptr()),
                ..Default::default()
            };
            let mut child = PROCESS_INFORMATION::default();
            // NULL does not inherit the caller's Actions environment: this API builds an
            // environment from the user profile instead. Preserve the current Unicode block
            // without serialising it (it may contain credentials).
            let environment = GetEnvironmentStringsW();
            if environment.is_null() {
                return Err("GetEnvironmentStringsW failed".into());
            }
            let created = CreateProcessWithTokenW(
                token,
                CREATE_PROCESS_LOGON_FLAGS(0),
                PCWSTR(executable.as_ptr()),
                Some(PWSTR(command.as_mut_ptr())),
                CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                Some(environment.0 as *const _),
                None,
                &startup,
                &mut child,
            );
            let _ = FreeEnvironmentStringsW(environment);
            created.map_err(ctx("CreateProcessWithTokenW(medium)"))?;
            eprintln!("medium child created: pid={}", child.dwProcessId);
            let _ = CloseHandle(child.hThread);
            let wait = WaitForSingleObject(child.hProcess, timeout_ms);
            let output = if wait == WAIT_OBJECT_0 {
                let mut code = 1u32;
                GetExitCodeProcess(child.hProcess, &mut code)
                    .map(|_| {
                        eprintln!(
                            "medium child exited: pid={}, code={code}",
                            child.dwProcessId
                        );
                        code
                    })
                    .map_err(ctx("GetExitCodeProcess"))
            } else {
                let _ = TerminateProcess(child.hProcess, 124);
                let stopped = WaitForSingleObject(child.hProcess, 2000) == WAIT_OBJECT_0;
                Err(format!(
                    "medium worker timed out; termination_confirmed={stopped}"
                ))
            };
            let _ = CloseHandle(child.hProcess);
            output
        })();
        let _ = CloseHandle(token);
        result
    }
}
