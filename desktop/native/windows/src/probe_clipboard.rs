//! Fictional clipboard payloads for the disposable CI probe only. No product protocol action.
use crate::{clipboard as clip, R};
use serde_json::{json, Value};

fn unicode(text: &str) -> clip::Entry {
    clip::Entry {
        format: clip::CF_UNICODE,
        data: crate::wide(text)
            .iter()
            .flat_map(|u| u.to_le_bytes())
            .collect(),
    }
}

pub fn state() -> R<Value> {
    let snapshot = clip::snapshot()?;
    let formats: Vec<clip::FormatInfo> = snapshot
        .entries
        .iter()
        .map(|e| clip::FormatInfo {
            format: e.format,
            name: clip::format_name(e.format),
            bytes: e.data.len(),
            fnv64: clip::fnv64(&e.data),
        })
        .collect();
    let text = snapshot
        .entries
        .iter()
        .find(|e| e.format == clip::CF_UNICODE)
        .map(|e| {
            let units: Vec<u16> = e
                .data
                .chunks_exact(2)
                .map(|b| u16::from_le_bytes([b[0], b[1]]))
                .take_while(|u| *u != 0)
                .collect();
            String::from_utf16_lossy(&units)
        });
    let exclusions: Vec<Value> = snapshot.entries.iter().filter_map(|e| {
        let name=clip::format_name(e.format);
        if ["ExcludeClipboardContentFromMonitorProcessing","CanIncludeInClipboardHistory","CanUploadToCloudClipboard"].contains(&name.as_str()) {
            Some(json!({"name":name,"dword":e.data.get(..4).map(|b| u32::from_le_bytes(b.try_into().unwrap()))}))
        } else { None }
    }).collect();
    Ok(
        json!({"sequence":snapshot.seq,"text":text,"formats":formats,"skipped":snapshot.skipped,"exclusions":exclusions}),
    )
}

pub fn seed(request: &Value) -> R<Value> {
    if std::env::var("GITHUB_ACTIONS").as_deref() != Ok("true") {
        return Err("clipboard seeding requires a disposable CI fixture".into());
    }
    let kind = request["kind"].as_str().unwrap_or("unicode");
    let mut entries = vec![unicode("SVI original clipboard")];
    match kind {
        "unicode" => {}
        "intervening" => entries = vec![unicode("SVI intervening clipboard")],
        "html" => {
            // CF_HTML offsets are UTF-8 byte offsets from the start of the complete payload.
            let prefix = "<html><body><!--StartFragment-->";
            let fragment = "<b>React</b> 中文🙂";
            let suffix = "<!--EndFragment--></body></html>";
            let header = |start, end, fragment_start, fragment_end| {
                format!(
                "Version:1.0\r\nStartHTML:{start:010}\r\nEndHTML:{end:010}\r\nStartFragment:{fragment_start:010}\r\nEndFragment:{fragment_end:010}\r\n"
            )
            };
            let start = header(0, 0, 0, 0).len();
            let fragment_start = start + prefix.len();
            let fragment_end = fragment_start + fragment.len();
            let end = fragment_end + suffix.len();
            entries.insert(
                0,
                clip::Entry {
                    format: clip::registered_format("HTML Format"),
                    data: format!(
                        "{}{prefix}{fragment}{suffix}\0",
                        header(start, end, fragment_start, fragment_end)
                    )
                    .into_bytes(),
                },
            );
        }
        "rtf" => entries.insert(
            0,
            clip::Entry {
                format: clip::registered_format("Rich Text Format"),
                data: b"{\\rtf1\\ansi {\\b React} \\u20013?\\u25991?}\0".to_vec(),
            },
        ),
        "dib" => {
            // BITMAPINFOHEADER, 2x2 bottom-up BGRA, BI_RGB. No real screenshot/image.
            let mut data = vec![0u8; 56];
            for (offset, value) in [(0, 40u32), (4, 2), (8, 2), (20, 16), (24, 3780), (28, 3780)] {
                data[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
            }
            data[12..14].copy_from_slice(&1u16.to_le_bytes());
            data[14..16].copy_from_slice(&32u16.to_le_bytes());
            data[40..].copy_from_slice(&[
                0, 0, 255, 255, 0, 255, 0, 255, 255, 0, 0, 255, 255, 255, 255, 255,
            ]);
            entries = vec![clip::Entry {
                format: clip::dib_format(),
                data,
            }];
        }
        "files" => {
            let file = request["fixture_file"]
                .as_str()
                .ok_or("fixture_file required")?;
            let file = std::fs::canonicalize(file).map_err(|e| e.to_string())?;
            let temporary =
                std::fs::canonicalize(std::env::var("RUNNER_TEMP").map_err(|e| e.to_string())?)
                    .map_err(|e| e.to_string())?;
            if !file.starts_with(temporary)
                || !file.is_file()
                || file.file_name().and_then(|n| n.to_str())
                    != Some("SmartVoiceInput clip fixture.txt")
            {
                return Err("only an owned temporary fictional file is allowed".into());
            }
            // DROPFILES: pFiles=20, fWide=TRUE, double-NUL terminated UTF-16 path list.
            let mut data = vec![0u8; 20];
            data[0..4].copy_from_slice(&20u32.to_le_bytes());
            data[16..20].copy_from_slice(&1u32.to_le_bytes());
            for unit in file.to_string_lossy().encode_utf16().chain([0, 0]) {
                data.extend_from_slice(&unit.to_le_bytes());
            }
            entries = vec![clip::Entry { format: 15, data }];
        }
        "gdi-private" => entries.insert(
            0,
            clip::Entry {
                // CF_GDIOBJFIRST..LAST are system-owned HGLOBAL objects; product rejects this class.
                format: 0x0301,
                data: b"SVI GDI fixture\0\0".to_vec(),
            },
        ),
        "oversize" => entries.insert(
            0,
            clip::Entry {
                format: clip::registered_format("SmartVoiceInput oversized fictional fixture"),
                data: vec![0x53; 33 * 1024 * 1024],
            },
        ),
        _ => return Err("unknown clipboard fixture kind".into()),
    }
    clip::write_entries(&entries)?;
    let snapshot = clip::snapshot()?;
    let expected: Vec<Value> = entries
        .iter()
        .map(|e| {
            json!({
                "format":e.format,"name":clip::format_name(e.format),"payload_bytes":e.data.len(),
                "payload_fnv64":clip::fnv64(&e.data),
                "readback_payload_matches":snapshot.entries.iter().any(|actual|
                    actual.format == e.format && actual.data.starts_with(&e.data))
            })
        })
        .collect();
    let mut result = state()?;
    result["fixture_kind"] = kind.into();
    result["expected_payloads"] = json!(expected);
    Ok(result)
}
