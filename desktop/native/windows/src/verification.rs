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

/// Chromium may expose an empty atomic edit as an object character or its accessible name.
/// Normalize only when independent ValuePattern, selection and embedded-child facts agree.
/// Literal U+FFFC, rich embedded objects, missing selection and any other provider remain intact.
pub fn normalize_empty_edit(
    read: &mut Readback,
    chromium_edit: bool,
    value: Option<&str>,
    no_embedded_children: bool,
    accessible_name: Option<&str>,
) {
    if chromium_edit
        && value == Some("")
        && no_embedded_children
        && (read.text == "\u{fffc}"
            || accessible_name.is_some_and(|name| !name.is_empty() && read.text == name))
        && read.caret_utf16 == Some(0)
        && read.selection_utf16_len == Some(0)
    {
        read.text.clear();
        read.source = "text_pattern+empty_value".into();
    }
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
    #[test]
    fn chromium_navigation_placeholder_needs_independent_empty_value_and_zero_selection() {
        let mut empty = read("\u{fffc}", Some(0), Some(0));
        normalize_empty_edit(&mut empty, true, Some(""), true, None);
        assert_eq!(
            expected_after(&empty, "中文🙂").unwrap(),
            Expected {
                text: "中文🙂".into(),
                caret_utf16: 4
            }
        );
        for (is_edit, value, children, caret, len) in [
            (true, Some("\u{fffc}"), true, Some(0), Some(0)),
            (true, None, true, Some(0), Some(0)),
            (false, Some(""), true, Some(0), Some(0)),
            (true, Some(""), false, Some(0), Some(0)),
            (true, Some(""), true, None, Some(0)),
            (true, Some(""), true, Some(0), Some(1)),
        ] {
            let mut r = read("\u{fffc}", caret, len);
            normalize_empty_edit(&mut r, is_edit, value, children, None);
            assert_eq!(r.text, "\u{fffc}");
        }
    }
    #[test]
    fn chromium_empty_accessible_name_is_not_document_content() {
        let label = "Fictional accessible label";
        let mut empty = read(label, Some(0), Some(0));
        normalize_empty_edit(&mut empty, true, Some(""), true, Some(label));
        assert_eq!(expected_after(&empty, "中文🙂").unwrap().text, "中文🙂");
        for (is_edit, value, no_children, name, caret, len) in [
            (true, Some(label), true, Some(label), Some(0), Some(0)),
            (
                true,
                Some(""),
                true,
                Some("another label"),
                Some(0),
                Some(0),
            ),
            (true, Some(""), true, None, Some(0), Some(0)),
            (false, Some(""), true, Some(label), Some(0), Some(0)),
            (true, Some(""), false, Some(label), Some(0), Some(0)),
            (true, None, true, Some(label), Some(0), Some(0)),
            (true, Some(""), true, Some(label), Some(1), Some(0)),
            (true, Some(""), true, Some(label), Some(0), Some(1)),
        ] {
            let mut actual = read(label, caret, len);
            normalize_empty_edit(&mut actual, is_edit, value, no_children, name);
            assert_eq!(actual.text, label);
        }
    }
}
