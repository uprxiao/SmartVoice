//! Modifier provenance/restoration policy, separate from the Win32 event transport.
//! The fixture tests here simulate hook flags; they do not claim physical keyboard execution.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct State {
    pub physical: u16,
    pub unknown: u16,
}

impl State {
    pub fn initial(held: u16) -> Self {
        Self {
            physical: 0,
            unknown: held,
        }
    }

    pub fn event(mut self, mask: u16, down: bool, injected: bool) -> Self {
        if injected || mask == 0 {
            return self;
        }
        self.unknown &= !mask;
        if down {
            self.physical |= mask;
        } else {
            self.physical &= !mask;
        }
        self
    }

    pub fn restore_mask(self, neutralised: u16) -> u16 {
        self.physical & neutralised
    }
    pub fn has_unknown_held(self, held: u16) -> bool {
        self.unknown & held != 0
    }
    pub fn packed(self) -> u32 {
        self.physical as u32 | ((self.unknown as u32) << 16)
    }
    pub fn from_packed(value: u32) -> Self {
        Self {
            physical: value as u16,
            unknown: (value >> 16) as u16,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const CTRL: u16 = 1;
    const SHIFT: u16 = 2;
    #[test]
    fn injected_neutralisation_does_not_erase_physical_provenance() {
        let held = State::default()
            .event(CTRL, true, false)
            .event(SHIFT, true, false);
        let neutralised = held.event(CTRL | SHIFT, false, true);
        assert_eq!(neutralised.restore_mask(CTRL | SHIFT), CTRL | SHIFT);
        assert_eq!(neutralised.restore_mask(SHIFT), SHIFT);
        assert_eq!(State::from_packed(neutralised.packed()), neutralised);
    }
    #[test]
    fn physical_release_before_restore_never_synthesises_that_key_down() {
        let state = State::default()
            .event(CTRL | SHIFT, true, false)
            .event(SHIFT, false, false);
        assert_eq!(state.restore_mask(CTRL | SHIFT), CTRL);
        assert_eq!(
            state.event(CTRL, false, false).restore_mask(CTRL | SHIFT),
            0
        );
        assert_eq!(
            State::default()
                .event(CTRL | SHIFT, true, true)
                .restore_mask(CTRL | SHIFT),
            0
        );
    }
    #[test]
    fn startup_state_is_unknown_until_a_noninjected_transition() {
        let initial = State::initial(CTRL | SHIFT);
        assert!(initial.has_unknown_held(CTRL));
        assert!(!initial.has_unknown_held(0));
        assert!(initial.event(CTRL, true, true).has_unknown_held(CTRL));
        let released = initial.event(CTRL | SHIFT, false, false);
        assert!(!released.has_unknown_held(CTRL | SHIFT));
        assert_eq!(released.restore_mask(CTRL | SHIFT), 0);
    }
}
