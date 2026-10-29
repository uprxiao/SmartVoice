//! Side-effect-free protocol and replay boundaries shared by the production helper tests.
use serde::Deserialize;
use std::collections::HashMap;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub id: u64,
    pub action: String,
    pub prompt: Option<bool>,
    pub target: Option<String>,
    pub insertion_id: Option<String>,
    pub text: Option<String>,
    #[serde(rename = "restoreFocus", default)]
    pub restore_focus: bool,
}

impl Request {
    pub fn valid(&self) -> bool {
        if self.id == 0 || self.id > 9_007_199_254_740_991 {
            return false;
        }
        match self.action.as_str() {
            "permission" | "capture" | "inputBounds" => true,
            "release" | "validate" => self
                .target
                .as_ref()
                .is_some_and(|s| !s.is_empty() && s.len() <= 256),
            "insert" | "insertCurrent" => {
                self.insertion_id
                    .as_ref()
                    .is_some_and(|s| !s.is_empty() && s.len() <= 256)
                    && self.text.as_ref().is_some_and(|s| {
                        !s.is_empty() && !s.contains('\0') && s.encode_utf16().count() <= 32000
                    })
                    && (self.action == "insertCurrent"
                        || self
                            .target
                            .as_ref()
                            .is_some_and(|s| !s.is_empty() && s.len() <= 256))
            }
            _ => false,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Effect {
    None,
    Unknown,
}
#[derive(Default)]
pub struct Executions(HashMap<String, Effect>);
impl Executions {
    // No eviction: forgetting an identity would permit an automatic replay.
    pub fn begin(&mut self, id: &str) -> Result<(), Effect> {
        if let Some(effect) = self.0.get(id) {
            return Err(*effect);
        }
        if self.0.len() >= 10000 {
            return Err(Effect::None);
        }
        self.0.insert(id.into(), Effect::Unknown);
        Ok(())
    }
    pub fn finish(&mut self, id: &str, effect: Effect) {
        self.0.insert(id.into(), effect);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn invalid_payloads_cannot_reach_dispatch() {
        for body in [
            json!({"id":1,"action":"probePaste"}),
            json!({"id":1,"action":"insertCurrent","text":"x"}),
            json!({"id":1,"action":"insertCurrent","text":"x","insertion_id":"a","restoreFocus":"yes"}),
            json!({"id":1,"action":"insertCurrent","text":"🙂".repeat(16001),"insertion_id":"a"}),
            json!({"id":1,"action":"insertCurrent","text":"A\u{0}B","insertion_id":"a"}),
            json!({"id":9007199254740992u64,"action":"permission"}),
            json!({"id":1,"action":"permission","data":{"prompt":true}}),
        ] {
            assert!(!serde_json::from_value::<Request>(body).is_ok_and(|r| r.valid()));
        }
        assert!(serde_json::from_value::<Request>(
            json!({"id":1,"action":"insertCurrent","text":"🙂".repeat(16000),"insertion_id":"a"})
        )
        .unwrap()
        .valid());
    }
    #[test]
    fn interrupted_and_successful_attempts_never_replay_and_zero_write_stays_zero() {
        let mut e = Executions::default();
        assert_eq!(e.begin("a"), Ok(()));
        assert_eq!(e.begin("a"), Err(Effect::Unknown));
        e.finish("a", Effect::None);
        assert_eq!(e.begin("a"), Err(Effect::None));
        for i in 1..10000 {
            assert!(e.begin(&format!("{i}")).is_ok());
        }
        assert_eq!(e.begin("full"), Err(Effect::None));
        assert_eq!(e.begin("a"), Err(Effect::None));
    }
}
