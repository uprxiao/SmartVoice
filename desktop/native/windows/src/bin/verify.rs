fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(2);
    }
}

fn run() -> svi_input_win::R<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("--summarize") && args.len() == 3 {
        use std::io::Write;
        let data = std::fs::read(&args[1]).map_err(|e| e.to_string())?;
        let cases: Vec<svi_input_win::report::Case> =
            serde_json::from_slice(&data).map_err(|e| e.to_string())?;
        let summary = svi_input_win::report::summary(&cases)?;
        let mut out = std::fs::File::create_new(&args[2]).map_err(|e| e.to_string())?;
        writeln!(
            out,
            "{}",
            serde_json::to_string_pretty(&summary).map_err(|e| e.to_string())?
        )
        .map_err(|e| e.to_string())?;
        return Ok(());
    }
    #[cfg(windows)]
    {
        let preflight_file =
            args.first().map(String::as_str) == Some("--preflight-file") && args.len() == 2;
        if (args.first().map(String::as_str) == Some("--preflight") && args.len() == 1)
            || preflight_file
        {
            use windows::Win32::UI::WindowsAndMessaging::{
                GetSystemMetrics, SM_CXSCREEN, SM_CYSCREEN,
            };
            let rid = svi_input_win::integrity::own_rid()?;
            let environment_present = ["CARGO_TARGET_DIR", "GITHUB_ACTIONS", "SVI_PROBE_EVIDENCE"]
                .iter()
                .map(|key| (*key, std::env::var_os(key).is_some()))
                .collect::<std::collections::BTreeMap<_, _>>();
            let report = serde_json::json!({"pid":std::process::id(),"integrity_rid":rid,
                "integrity":svi_input_win::integrity::level_name(rid),
                "input_desktop_accessible":svi_input_win::foreground::input_desktop_accessible(),
                "foreground":svi_input_win::foreground::foreground(),
                "screen_width_px":unsafe { GetSystemMetrics(SM_CXSCREEN) },
                "screen_height_px":unsafe { GetSystemMetrics(SM_CYSCREEN) },
                "environment_present": environment_present });
            if preflight_file {
                use std::io::Write;
                let mut out = std::fs::File::create_new(&args[1]).map_err(|e| e.to_string())?;
                writeln!(out, "{}", report).map_err(|e| e.to_string())?;
            } else {
                println!("{report}");
            }
            return Ok(());
        }
        if args.first().map(String::as_str) == Some("--medium") && args.len() >= 2 {
            let code =
                svi_input_win::launch::medium(std::path::Path::new(&args[1]), &args[2..], 600_000)?;
            std::process::exit(i32::try_from(code).unwrap_or(2));
        }
    }
    Err("usage: verify --summarize cases.json new-summary.json | (Windows) --preflight | --medium PROGRAM ARGS...".into())
}
