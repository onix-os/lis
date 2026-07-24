//! Semantic validation — SPEC.md §19, the rules a JSON Schema cannot express.

use std::collections::BTreeSet;

use crate::document::{Document, Firmware, Fs, Loader, Role};

/// One violated rule: a JSON-path-ish location and what went wrong.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Issue {
    pub path: String,
    pub message: String,
}

impl std::fmt::Display for Issue {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.path, self.message)
    }
}

fn issue(issues: &mut Vec<Issue>, path: &str, message: impl Into<String>) {
    issues.push(Issue {
        path: path.to_string(),
        message: message.into(),
    });
}

/// Check every §19 rule. Empty result = semantically valid.
pub fn validate(doc: &Document) -> Vec<Issue> {
    let mut issues = Vec::new();

    // 1. Version.
    if !doc.lis.starts_with("0.1.") {
        issue(
            &mut issues,
            "$.lis",
            format!("unsupported version {:?} (this crate implements 0.1.x)", doc.lis),
        );
    }

    // Extensions must be x- namespaced.
    for key in doc.extensions.keys() {
        if !key.starts_with("x-") {
            issue(
                &mut issues,
                &format!("$.{key}"),
                "unknown top-level key (extensions must start with \"x-\")",
            );
        }
    }

    let disk_ids: BTreeSet<&str> = doc
        .target
        .iter()
        .flat_map(|t| t.disks.iter())
        .map(|d| d.id.as_str())
        .collect();

    let Some(storage) = &doc.storage else {
        validate_non_storage(doc, &mut issues, false);
        return issues;
    };

    // 3/4. References, ids, one `rest` per disk.
    let mut part_ids: BTreeSet<&str> = BTreeSet::new();
    let mut root_providers = 0usize;
    let mut btrfs_root = false;
    let mut rest_per_disk: std::collections::BTreeMap<&str, usize> = Default::default();

    for (i, part) in storage.partitions.iter().enumerate() {
        let path = format!("$.storage.partitions[{i}]");
        if !disk_ids.contains(part.disk.as_str()) {
            issue(&mut issues, &path, format!("undeclared disk handle {:?}", part.disk));
        }
        if let Some(id) = &part.id {
            if !part_ids.insert(id.as_str()) {
                issue(&mut issues, &path, format!("duplicate id {id:?}"));
            }
        }
        if part.size.is_none() && part.existing.is_none() {
            issue(&mut issues, &path, "needs either a size or an existing match");
        }
        if part.size.map(|s| s.is_rest()).unwrap_or(false) {
            *rest_per_disk.entry(part.disk.as_str()).or_default() += 1;
        }
        let mountpoint = part.mountpoint.clone().or_else(|| {
            (part.role == Some(Role::Root)).then(|| "/".to_string())
        });
        let is_root = mountpoint.as_deref() == Some("/")
            || part.subvolumes.iter().any(|s| s.mountpoint == "/");
        if is_root {
            root_providers += 1;
            if part.fs == Some(Fs::Btrfs) {
                btrfs_root = true;
            }
        }
    }
    for (disk, n) in &rest_per_disk {
        if *n > 1 {
            issue(
                &mut issues,
                "$.storage.partitions",
                format!("disk {disk:?} has {n} partitions sized \"rest\" (max one)"),
            );
        }
    }

    let mut crypt_ids: BTreeSet<&str> = BTreeSet::new();
    for (i, crypt) in storage.encryption.iter().enumerate() {
        let path = format!("$.storage.encryption[{i}]");
        if !part_ids.contains(crypt.over.as_str()) {
            issue(&mut issues, &path, format!("`over` references unknown id {:?}", crypt.over));
        }
        crypt_ids.insert(crypt.id.as_str());
    }

    let referable: BTreeSet<&str> = part_ids.union(&crypt_ids).copied().collect();
    for (i, group) in storage.lvm.iter().enumerate() {
        let path = format!("$.storage.lvm[{i}]");
        for dev in &group.devices {
            if !referable.contains(dev.as_str()) && !dev.starts_with("md:") {
                issue(&mut issues, &path, format!("unknown device {dev:?}"));
            }
        }
        let mut rests = 0usize;
        for vol in &group.volumes {
            if vol.size.map(|s| s.is_rest()).unwrap_or(false) {
                rests += 1;
            }
            let is_root = vol.mountpoint.as_deref() == Some("/")
                || vol.subvolumes.iter().any(|s| s.mountpoint == "/");
            if is_root {
                root_providers += 1;
                if vol.fs == Some(Fs::Btrfs) {
                    btrfs_root = true;
                }
            }
        }
        if rests > 1 {
            issue(
                &mut issues,
                &path,
                format!("{rests} volumes sized \"rest\" (max one per pool)"),
            );
        }
    }
    for (i, raid) in storage.raid.iter().enumerate() {
        let path = format!("$.storage.raid[{i}]");
        for dev in raid.devices.iter().chain(raid.spares.iter()) {
            if !referable.contains(dev.as_str()) {
                issue(&mut issues, &path, format!("unknown device {dev:?}"));
            }
        }
    }

    // 5. Exactly one root.
    if root_providers != 1 {
        issue(
            &mut issues,
            "$.storage",
            format!("expected exactly one filesystem mounted at \"/\", found {root_providers}"),
        );
    }

    // 9. Snapshots need a btrfs root.
    if storage.snapshots.as_ref().map(|s| s.enabled).unwrap_or(false) && !btrfs_root {
        issue(&mut issues, "$.storage.snapshots", "requires a btrfs root");
    }

    validate_non_storage(doc, &mut issues, true);
    issues
}

fn validate_non_storage(doc: &Document, issues: &mut Vec<Issue>, _has_storage: bool) {
    // 6. Firmware/loader coherence.
    let firmware = doc
        .target
        .as_ref()
        .and_then(|t| t.firmware)
        .unwrap_or(Firmware::Auto);
    if doc.boot.as_ref().and_then(|b| b.loader) == Some(Loader::SystemdBoot)
        && firmware == Firmware::Bios
    {
        issue(issues, "$.boot.loader", "systemd-boot requires uefi firmware");
    }

    // 7. No plaintext secrets.
    for (i, user) in doc.users.iter().enumerate() {
        if let Some(hash) = user.password.as_ref().and_then(|p| p.hash.as_deref()) {
            if !hash.starts_with('$') {
                issue(
                    issues,
                    &format!("$.users[{i}].password.hash"),
                    "not a crypt(3) hash (plaintext passwords are forbidden)",
                );
            }
        }
    }
    if let Some(registration) = &doc.registration {
        if let Some(token) = &registration.token {
            if !["file:", "env:", "seed:"]
                .iter()
                .any(|p| token.from.starts_with(p))
            {
                issue(
                    issues,
                    "$.registration.token.from",
                    "must be a file:, env:, or seed: reference",
                );
            }
        }
    }

    // 10. Desktop constraints.
    if let Some(desktop) = &doc.desktop {
        let role = doc
            .software
            .as_ref()
            .and_then(|s| s.role.as_deref())
            .unwrap_or("");
        if !role.starts_with("desktop:") {
            issue(issues, "$.desktop", "requires software.role \"desktop:*\"");
        }
        if let Some(autologin) = &desktop.autologin {
            match doc.users.iter().find(|u| &u.name == autologin) {
                None => issue(
                    issues,
                    "$.desktop.autologin",
                    format!("names unknown user {autologin:?}"),
                ),
                Some(user) => {
                    if user.password.as_ref().and_then(|p| p.locked) == Some(true) {
                        issue(
                            issues,
                            "$.desktop.autologin",
                            format!("user {autologin:?} has a locked password"),
                        );
                    }
                }
            }
        }
    }
}
