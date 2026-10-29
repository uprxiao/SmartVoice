#[cfg(not(windows))]
fn main() {
    eprintln!(
        "Windows technical probe: execution requires Windows; product input is not connected"
    );
    std::process::exit(2);
}

#[cfg(windows)]
fn main() {
    use serde_json::{json, Value};
    use std::io::{BufRead, Read, Write};
    let _modifier_watcher = match svi_input_win::modifier_watch::Watcher::start() {
        Ok(watcher) => watcher,
        Err(_) => {
            eprintln!("Windows probe: modifier hook initialization failed");
            std::process::exit(2);
        }
    };
    let uia = match svi_input_win::uia::Uia::new() {
        Ok(uia) => uia,
        Err(_) => {
            eprintln!("Windows probe: UI Automation initialization failed");
            std::process::exit(2);
        }
    };
    let mut reader = std::io::BufReader::new(std::io::stdin().lock());
    let mut stdout = std::io::stdout().lock();
    const MAX: usize = 1024 * 1024;
    loop {
        let mut bytes = Vec::new();
        let size = match (&mut reader)
            .take((MAX + 1) as u64)
            .read_until(b'\n', &mut bytes)
        {
            Ok(0) => break,
            Ok(n) => n,
            Err(_) => break,
        };
        if size > MAX {
            eprintln!("Windows probe: request exceeds limit");
            std::process::exit(2);
        }
        let request: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
        let id = request["id"]
            .as_u64()
            .filter(|n| *n <= 9_007_199_254_740_991)
            .unwrap_or(0);
        let result = if id == 0 {
            Err("positive numeric id required".into())
        } else {
            svi_input_win::probe_protocol::dispatch(&uia, &request)
        };
        let response = match result {
            Ok(Value::Object(mut value)) => {
                value.insert("id".into(), id.into());
                Value::Object(value)
            }
            Ok(_) => json!({"id":id,"error":{"code":"PROBE_PROTOCOL","effect":"unknown"}}),
            Err(error) => json!({"id":id,"error":{"code":"PROBE_ERROR","message":error,
                "effect":if request["action"] == "probePaste" { "unknown" } else { "none" }}}),
        };
        if serde_json::to_writer(&mut stdout, &response).is_err()
            || stdout.write_all(b"\n").is_err()
            || stdout.flush().is_err()
        {
            break;
        }
    }
}
