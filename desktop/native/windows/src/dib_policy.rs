//! Bounded BI_RGB32 DIB view used to prove a derived bitmap is reproducible.
//! Unsupported headers, profiles, palettes and compressed/bitfield images stay unsupported.
pub struct Rgb32<'a> {
    pub width: i32,
    pub height: i32,
    pub pixels: &'a [u8],
}
pub fn rgb32(data: &[u8]) -> Option<Rgb32<'_>> {
    let u32_at = |offset: usize| {
        Some(u32::from_le_bytes(
            data.get(offset..offset + 4)?.try_into().ok()?,
        ))
    };
    let u16_at = |offset: usize| {
        Some(u16::from_le_bytes(
            data.get(offset..offset + 2)?.try_into().ok()?,
        ))
    };
    let header = u32_at(0)? as usize;
    if !matches!(header, 40 | 124) || data.len() < header {
        return None;
    }
    let width = u32_at(4)? as i32;
    let height = u32_at(8)? as i32;
    if width <= 0
        || height == 0
        || height == i32::MIN
        || u16_at(12)? != 1
        || u16_at(14)? != 32
        || u32_at(16)? != 0
        || u32_at(32)? != 0
    {
        return None;
    }
    // V5 embedded/linked color profiles need a separate fidelity strategy.
    if header == 124 && (u32_at(56)? != 0x73524742 || u32_at(112)? != 0 || u32_at(116)? != 0) {
        return None;
    }
    let bytes = (width as usize)
        .checked_mul(height.unsigned_abs() as usize)?
        .checked_mul(4)?;
    if bytes > 32 * 1024 * 1024 || (u32_at(20)? != 0 && u32_at(20)? as usize != bytes) {
        return None;
    }
    Some(Rgb32 {
        width,
        height,
        pixels: data.get(header..header.checked_add(bytes)?)?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn dib(width: i32, height: i32) -> Vec<u8> {
        let mut data = vec![0; 56];
        data[..4].copy_from_slice(&40u32.to_le_bytes());
        data[4..8].copy_from_slice(&width.to_le_bytes());
        data[8..12].copy_from_slice(&height.to_le_bytes());
        data[12..14].copy_from_slice(&1u16.to_le_bytes());
        data[14..16].copy_from_slice(&32u16.to_le_bytes());
        data
    }
    #[test]
    fn rejects_hostile_dimensions_truncated_pixels_and_unsupported_layouts() {
        assert_eq!(rgb32(&dib(2, -2)).unwrap().pixels.len(), 16);
        for (width, height) in [(0, 2), (-2, 2), (2, 0), (2, i32::MIN), (i32::MAX, i32::MAX)] {
            assert!(rgb32(&dib(width, height)).is_none());
        }
        assert!(rgb32(&dib(2, 2)[..55]).is_none());
        for (offset, value) in [(0, 108u32), (16, 3), (20, 1), (32, 1)] {
            let mut data = dib(2, 2);
            data[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
            assert!(rgb32(&data).is_none());
        }
    }
}
