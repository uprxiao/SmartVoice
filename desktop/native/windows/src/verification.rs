//! Platform-independent read-back decision. An accepted SendInput count is never proof of delivery.
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Readback {
    pub source: String,
    pub text: String,
    pub caret_utf16: Option<usize>,
    pub selection_utf16_len: Option<usize>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Expected {
    pub text: String,
    pub caret_utf16: usize,
}

pub fn expected_after(before: &Readback, inserted: &str) -> Option<Expected> {
    let start = before.caret_utf16?;
    let end = start.checked_add(before.selection_utf16_len?)?;
    let units: Vec<u16> = before.text.encode_utf16().collect();
    // Reject offsets that split a surrogate pair as well as offsets beyond the document.
    let prefix = String::from_utf16(units.get(..start)?).ok()?;
    let suffix = String::from_utf16(units.get(end..)?).ok()?;
    Some(Expected {
        text: format!("{prefix}{inserted}{suffix}"),
        caret_utf16: start.checked_add(inserted.encode_utf16().count())?,
    })
}

pub fn exactly_matches(expected: Option<&Expected>, after: Option<&Readback>) -> bool {
    matches!((expected, after), (Some(e), Some(a))
        if a.text == e.text && a.caret_utf16 == Some(e.caret_utf16)
            && a.selection_utf16_len == Some(0))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn read(text: &str, caret: Option<usize>, len: Option<usize>) -> Readback {
        Readback {
            source: "fixture".into(),
            text: text.into(),
            caret_utf16: caret,
            selection_utf16_len: len,
        }
    }
    #[test]
    fn replaces_selection_and_preserves_suffix_with_non_bmp_text() {
        let expected = expected_after(&read("A🙂ReactZ", Some(3), Some(5)), "中文🙂").unwrap();
        assert_eq!(expected.text, "A🙂中文🙂Z");
        assert_eq!(expected.caret_utf16, 7);
        assert!(exactly_matches(
            Some(&expected),
            Some(&read("A🙂中文🙂Z", Some(7), Some(0)))
        ));
    }
    #[test]
    fn missing_caret_never_confirms_a_write() {
        assert!(expected_after(&read("React", None, Some(0)), "X").is_none());
        let e = expected_after(&read("React", Some(5), Some(0)), "X").unwrap();
        assert!(!exactly_matches(
            Some(&e),
            Some(&read("ReactX", None, None))
        ));
    }
    #[test]
    fn rejects_split_surrogate_and_invalid_selection() {
        assert!(expected_after(&read("🙂", Some(1), Some(0)), "X").is_none());
        assert!(expected_after(&read("A", Some(0), Some(9)), "X").is_none());
        assert!(expected_after(&read("A", Some(usize::MAX), Some(1)), "X").is_none());
    }
    #[test]
    fn lost_suffix_wrong_caret_or_remaining_selection_are_unverified() {
        let e = expected_after(&read("ABCD", Some(1), Some(2)), "X").unwrap();
        for a in [
            read("AX", Some(2), Some(0)),
            read("AXD", Some(3), Some(0)),
            read("AXD", Some(2), Some(1)),
        ] {
            assert!(!exactly_matches(Some(&e), Some(&a)));
        }
        assert!(!exactly_matches(Some(&e), None));
        assert!(!exactly_matches(None, Some(&read("AXD", Some(2), Some(0)))));
    }
}
