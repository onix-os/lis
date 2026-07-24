//! The LIS size grammar: `"<n>MiB" | "<n>GiB" | "<n>TiB" | "<n>%" | "rest"`.

use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

/// A partition/volume size as the spec writes it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(try_from = "String", into = "String")]
pub enum Size {
    MiB(u64),
    GiB(u64),
    TiB(u64),
    Percent(u8),
    /// Whatever space the fixed-size siblings leave over (max one per disk/pool).
    /// Also the `Default`, so containing structs can derive it.
    #[default]
    Rest,
}

impl Size {
    /// Approximate value in whole GiB (percent has no absolute answer → None).
    pub fn as_gib(&self) -> Option<u64> {
        match self {
            Size::MiB(n) => Some(n.div_ceil(1024)),
            Size::GiB(n) => Some(*n),
            Size::TiB(n) => Some(n * 1024),
            Size::Percent(_) | Size::Rest => None,
        }
    }

    pub fn is_rest(&self) -> bool {
        matches!(self, Size::Rest)
    }
}

impl fmt::Display for Size {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Size::MiB(n) => write!(f, "{n}MiB"),
            Size::GiB(n) => write!(f, "{n}GiB"),
            Size::TiB(n) => write!(f, "{n}TiB"),
            Size::Percent(n) => write!(f, "{n}%"),
            Size::Rest => write!(f, "rest"),
        }
    }
}

impl FromStr for Size {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        if s == "rest" {
            return Ok(Size::Rest);
        }
        if let Some(n) = s.strip_suffix('%') {
            let n: u8 = n.parse().map_err(|_| bad(s))?;
            if n == 0 || n > 100 {
                return Err(bad(s));
            }
            return Ok(Size::Percent(n));
        }
        for (suffix, ctor) in [
            ("MiB", Size::MiB as fn(u64) -> Size),
            ("GiB", Size::GiB as fn(u64) -> Size),
            ("TiB", Size::TiB as fn(u64) -> Size),
        ] {
            if let Some(n) = s.strip_suffix(suffix) {
                return n.parse().map(ctor).map_err(|_| bad(s));
            }
        }
        Err(bad(s))
    }
}

fn bad(s: &str) -> String {
    format!("invalid size {s:?}: expected <n>MiB/<n>GiB/<n>TiB, <n>%, or \"rest\"")
}

impl TryFrom<String> for Size {
    type Error = String;
    fn try_from(value: String) -> Result<Self, Self::Error> {
        value.parse()
    }
}

impl From<Size> for String {
    fn from(size: Size) -> Self {
        size.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_and_prints_all_forms() {
        for (text, size) in [
            ("512MiB", Size::MiB(512)),
            ("8GiB", Size::GiB(8)),
            ("2TiB", Size::TiB(2)),
            ("50%", Size::Percent(50)),
            ("rest", Size::Rest),
        ] {
            assert_eq!(text.parse::<Size>().unwrap(), size);
            assert_eq!(size.to_string(), text);
        }
        assert!("8GB".parse::<Size>().is_err());
        assert!("0%".parse::<Size>().is_err());
        assert!("101%".parse::<Size>().is_err());
    }

    #[test]
    fn gib_conversion_rounds_up() {
        assert_eq!(Size::MiB(1024).as_gib(), Some(1));
        assert_eq!(Size::MiB(1025).as_gib(), Some(2));
        assert_eq!(Size::TiB(1).as_gib(), Some(1024));
        assert_eq!(Size::Rest.as_gib(), None);
    }
}
