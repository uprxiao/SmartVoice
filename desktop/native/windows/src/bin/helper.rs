#[cfg(not(windows))]
fn main() {
    eprintln!("Native input requires Windows");
    std::process::exit(2);
}

#[cfg(windows)]
fn main() {
    use serde_json::{json, Value};
    use std::io::{BufRead, Read, Write};
    use svi_input_win::{input_policy::Request, product::Product};
    use windows::Win32::UI::HiDpi::{
        SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
    };
    let args: Vec<String> = std::env::args().collect();
    let parent = args
        .windows(2)
        .find(|a| a[0] == "--parent-pid")
        .and_then(|a| a[1].parse::<u32>().ok())
        .filter(|p| *p > 0);
    let Some(parent) = parent else {
        eprintln!("Parent identity required");
        std::process::exit(2);
    };
    let Some(birth) = svi_input_win::integrity::process_birth(parent) else {
        std::process::exit(2);
    };
    std::thread::spawn(move || loop {
        std::thread::sleep(std::time::Duration::from_millis(500));
        if svi_input_win::integrity::process_birth(parent) != Some(birth) {
            std::process::exit(0);
        }
    });
    unsafe {
        let _ = SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    }
    let Ok(_watcher) = svi_input_win::modifier_watch::Watcher::start() else {
        eprintln!("Modifier initialization failed");
        std::process::exit(2);
    };
    let Ok(uia) = svi_input_win::uia::Uia::new() else {
        eprintln!("UI Automation initialization failed");
        std::process::exit(2);
    };
    let mut product = Product::new(uia, parent);
    let mut reader = std::io::BufReader::new(std::io::stdin().lock());
    let mut stdout = std::io::stdout().lock();
    const MAX: usize = 1024 * 1024;
    loop {
        let mut bytes = Vec::new();
        match (&mut reader)
            .take((MAX + 1) as u64)
            .read_until(b'\n', &mut bytes)
        {
            Ok(0) | Err(_) => break,
            Ok(size) if size > MAX => break,
            _ => {}
        }
        let parsed = serde_json::from_slice::<Request>(&bytes);
        let (id, mut response) = match parsed {
            Ok(r) if r.valid() => (r.id, product.dispatch(&r)),
            _ => {
                let id = serde_json::from_slice::<Value>(&bytes)
                    .ok()
                    .and_then(|v| v["id"].as_u64())
                    .filter(|n| *n <= 9_007_199_254_740_991)
                    .unwrap_or(0);
                (
                    id,
                    json!({"error":{"code":"NATIVE_REQUEST_INVALID","message":"输入请求无效","effect":"none"}}),
                )
            }
        };
        response["id"] = id.into();
        if serde_json::to_writer(&mut stdout, &response).is_err()
            || stdout.write_all(b"\n").is_err()
            || stdout.flush().is_err()
        {
            break;
        }
    }
}
