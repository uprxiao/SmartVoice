//! Report gate and Windows command-line quoting can be tested on the development host.
use crate::R;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeSet;

pub const BLOCKING: [&str; 9] = [
    "V-01", "V-02", "V-03", "V-04", "V-07", "V-08", "V-09", "V-10", "V-14",
];

#[derive(Debug, Serialize, Deserialize)]
pub struct Case {
    pub id: String,
    pub status: String,
    pub detail: Value,
}

pub fn summary(cases: &[Case]) -> R<Value> {
    let mut seen = BTreeSet::new();
    for case in cases {
        let valid_id = (1..=15).any(|n| case.id == format!("V-{n:02}"));
        if !valid_id || !seen.insert(&case.id) {
            return Err(format!("invalid/duplicate case: {}", case.id));
        }
        if !["pass", "fail", "blocked", "not_run", "partial"].contains(&case.status.as_str()) {
            return Err(format!("invalid status: {}", case.status));
        }
    }
    let missing: Vec<_> = (1..=15)
        .map(|n| format!("V-{n:02}"))
        .filter(|id| !seen.contains(id))
        .collect();
    let blocking_unpassed: Vec<_> = BLOCKING
        .iter()
        .filter(|id| !cases.iter().any(|c| c.id == **id && c.status == "pass"))
        .collect();
    Ok(
        serde_json::json!({"planned":15,"reported":cases.len(),"missing":missing,
        "passed":cases.iter().filter(|c| c.status == "pass").count(),
        "blocking_planned":9,"blocking_unpassed":blocking_unpassed,
        "technical_gate_passed":missing.is_empty() && blocking_unpassed.is_empty(),
        "product_s4_authorized":false,"product_s5_passed":false}),
    )
}

pub fn quote_windows_arg(value: &str) -> String {
    let mut out = String::from("\"");
    let mut slashes = 0;
    for c in value.chars() {
        if c == '\\' {
            slashes += 1;
            continue;
        }
        if c == '"' {
            out.push_str(&"\\".repeat(slashes * 2 + 1));
        } else {
            out.push_str(&"\\".repeat(slashes));
        }
        slashes = 0;
        out.push(c);
    }
    out.push_str(&"\\".repeat(slashes * 2));
    out.push('"');
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    fn all() -> Vec<Case> {
        (1..=15)
            .map(|n| Case {
                id: format!("V-{n:02}"),
                status: "pass".into(),
                detail: Value::Null,
            })
            .collect()
    }
    #[test]
    fn never_passes_a_missing_blocked_partial_or_duplicate_blocking_case() {
        let mut cases = all();
        assert_eq!(summary(&cases).unwrap()["technical_gate_passed"], true);
        for status in ["blocked", "partial", "not_run", "fail"] {
            cases[3].status = status.into();
            assert_eq!(summary(&cases).unwrap()["technical_gate_passed"], false);
        }
        cases.remove(3);
        assert_eq!(summary(&cases).unwrap()["technical_gate_passed"], false);
        cases.push(Case {
            id: "V-01".into(),
            status: "pass".into(),
            detail: Value::Null,
        });
        assert!(summary(&cases).is_err());
    }
    #[test]
    fn unrun_nonblocking_cases_remain_in_the_denominator() {
        let mut cases = all();
        cases[4].status = "not_run".into();
        let s = summary(&cases).unwrap();
        assert_eq!(s["planned"], 15);
        assert_eq!(s["passed"], 14);
        assert_eq!(s["product_s5_passed"], false);
    }
    #[test]
    fn command_line_keeps_spaces_quotes_and_trailing_backslashes_inside_one_argument() {
        assert_eq!(quote_windows_arg(""), "\"\"");
        assert_eq!(quote_windows_arg("路径 with space"), "\"路径 with space\"");
        assert_eq!(quote_windows_arg("a\"b"), "\"a\\\"b\"");
        assert_eq!(quote_windows_arg("C:\\folder\\"), "\"C:\\folder\\\\\"");
    }
}
