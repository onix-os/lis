//! # lis — Linux Installation Specification, reference implementation
//!
//! Typed model of a [LIS](https://github.com/onix-os/lis) document, JSON
//! emit/parse, and the SPEC §19 semantic validation.
//!
//! ```
//! use lis::{Document, System};
//!
//! let mut doc = Document::new();
//! doc.system = Some(System {
//!     hostname: Some("tron".into()),
//!     timezone: Some("Europe/Amsterdam".into()),
//!     ..Default::default()
//! });
//! let json = doc.to_json().unwrap();
//! let back = Document::from_json(&json).unwrap();
//! assert_eq!(doc, back);
//! assert!(lis::validate(&back).is_empty() || !back.storage.is_none());
//! ```

mod document;

mod size;
mod validate;

pub use document::*;
pub use size::Size;
pub use validate::{validate, Issue};

impl Document {
    /// Pretty-printed canonical JSON (the wire format).
    pub fn to_json(&self) -> Result<String, String> {
        serde_json::to_string_pretty(self)
            .map(|s| s + "\n")
            .map_err(|err| format!("failed to serialize LIS document: {err}"))
    }

    /// Parse a document and enforce the version gate (§2.1): documents with
    /// an unsupported major version are rejected outright.
    pub fn from_json(text: &str) -> Result<Self, String> {
        let doc: Document = serde_json::from_str(text)
            .map_err(|err| format!("not a valid LIS document: {err}"))?;
        if !doc.lis.starts_with("0.1.") {
            return Err(format!(
                "unsupported LIS version {} (this crate implements 0.1.x)",
                doc.lis
            ));
        }
        Ok(doc)
    }

    /// Parse + validate in one step; any semantic issue is an error.
    pub fn from_json_validated(text: &str) -> Result<Self, String> {
        let doc = Self::from_json(text)?;
        let issues = validate(&doc);
        if issues.is_empty() {
            Ok(doc)
        } else {
            Err(issues
                .iter()
                .map(ToString::to_string)
                .collect::<Vec<_>>()
                .join("; "))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn example(name: &str) -> String {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../docs/examples/");
        std::fs::read_to_string(format!("{path}{name}")).unwrap()
    }

    #[test]
    fn repo_json_examples_parse_validate_and_roundtrip() {
        for name in ["server-btrfs.lis.json", "server-lvm-pool.lis.json"] {
            let text = example(name);
            let doc = Document::from_json(&text).unwrap_or_else(|e| panic!("{name}: {e}"));
            let issues = validate(&doc);
            assert!(issues.is_empty(), "{name}: {issues:?}");
            let again = Document::from_json(&doc.to_json().unwrap()).unwrap();
            assert_eq!(doc, again, "{name} did not roundtrip");
        }
    }

    #[test]
    fn repo_yaml_example_parses_and_validates() {
        let text = example("desktop-encrypted.lis.yaml");
        let value: serde_json::Value = serde_yaml::from_str(&text).unwrap();
        let doc: Document = serde_json::from_value(value).unwrap();
        assert_eq!(doc.lis, "0.1.0");
        let issues = validate(&doc);
        assert!(issues.is_empty(), "{issues:?}");
        assert!(doc.storage.as_ref().unwrap().snapshots.as_ref().unwrap().enabled);
        assert_eq!(doc.desktop.as_ref().unwrap().autologin.as_deref(), Some("bresilla"));
    }

    #[test]
    fn validation_catches_the_spec_rules() {
        let text = example("server-lvm-pool.lis.json");
        let mut doc = Document::from_json(&text).unwrap();

        // Two roots: also mount the doc volume at /.
        doc.storage.as_mut().unwrap().lvm[0].volumes[2].mountpoint = Some("/".into());
        assert!(validate(&doc).iter().any(|i| i.message.contains("exactly one")));

        // Plaintext password.
        let mut doc = Document::from_json(&text).unwrap();
        doc.users[0].password = Some(Password {
            hash: Some("hunter2".into()),
            locked: None,
        });
        assert!(validate(&doc).iter().any(|i| i.message.contains("crypt(3)")));

        // systemd-boot on BIOS.
        let mut doc = Document::from_json(&text).unwrap();
        doc.target.as_mut().unwrap().firmware = Some(Firmware::Bios);
        assert!(validate(&doc).iter().any(|i| i.message.contains("uefi")));

        // Dangling LVM device.
        let mut doc = Document::from_json(&text).unwrap();
        doc.storage.as_mut().unwrap().lvm[0].devices.push("p-ghost".into());
        assert!(validate(&doc).iter().any(|i| i.message.contains("unknown device")));

        // Non-x extension key.
        let mut doc = Document::from_json(&text).unwrap();
        doc.extensions.insert("bogus".into(), serde_json::json!({}));
        assert!(validate(&doc).iter().any(|i| i.message.contains("x-")));
    }

    #[test]
    fn version_gate_rejects_future_majors() {
        assert!(Document::from_json(r#"{ "lis": "2.0.0" }"#)
            .unwrap_err()
            .contains("unsupported"));
    }

    #[test]
    fn secure_boot_tristate_serializes_heterogeneously() {
        let mut doc = Document::new();
        doc.boot = Some(Boot { secure_boot: Some(SecureBoot::On), ..Default::default() });
        let json = doc.to_json().unwrap();
        assert!(json.contains("\"secure_boot\": true"));
        doc.boot.as_mut().unwrap().secure_boot = Some(SecureBoot::Auto);
        assert!(doc.to_json().unwrap().contains("\"secure_boot\": \"auto\""));
        let parsed = Document::from_json(&json).unwrap();
        assert_eq!(parsed.boot.unwrap().secure_boot, Some(SecureBoot::On));
    }
}
