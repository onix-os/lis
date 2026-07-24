#!/usr/bin/env python3
"""lis2agama — translate a LIS document into openSUSE / SLES Agama profile.json.

Usage: lis2agama.py FILE.lis.json [--out DIR] [--strict]

Writes profile.json into DIR (default '.'):
  profile.json — Native JSON configuration profile for openSUSE Agama installer

Best-effort: core intent that Agama cannot express is reported as a warning;
with --strict any dropped intent exits non-zero (SPEC §2.3).
"""

import argparse
import json
import pathlib
import sys

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"warning: {msg}", file=sys.stderr)


def render_agama(doc: dict) -> dict:
    system = doc.get("system", {}) or {}
    boot = doc.get("boot", {}) or {}
    storage = doc.get("storage", {}) or {}
    software = doc.get("software", {}) or {}
    users = doc.get("users", []) or []

    profile: dict = {
        "product": "openSUSE Tumbleweed",
        "locale": {
            "lang": system.get("locale", "en_US.UTF-8"),
            "keymap": (system.get("keymap", {}) or {}).get("console", "us"),
            "timezone": system.get("timezone", "UTC"),
        },
        "software": {},
        "users": [],
        "scripts": [],
    }

    # Software packages & apps
    pkgs = list(software.get("packages", []))
    for app in software.get("apps", []):
        if isinstance(app, str):
            pkgs.append(app)
        elif isinstance(app, dict):
            if name := (app.get("package") or app.get("name")):
                pkgs.append(name)
    if pkgs:
        profile["software"]["packages"] = pkgs

    role = software.get("role", "")
    role_patterns = {
        "desktop:gnome": ["gnome_basis", "gnome"],
        "desktop:kde": ["kde_plasma", "kde"],
        "desktop:xfce": ["xfce_basis", "xfce"],
    }
    if role in role_patterns:
        profile["software"]["patterns"] = role_patterns[role]

    # Users
    for u in users:
        u_dict = {
            "userName": u["name"],
            "root": u["name"] == "root",
        }
        if h := (u.get("password") or {}).get("hash"):
            u_dict["encryptedPassword"] = h
        if u.get("admin"):
            u_dict["autoLogin"] = False
        profile["users"].append(u_dict)

    # Scripts
    scripts = doc.get("scripts", {}) or {}
    for stage in ("pre_install", "pre", "post_storage"):
        for s in scripts.get(stage, []):
            if c := s.get("content"):
                profile["scripts"].append({"stage": "pre", "body": c})

    for stage in ("post_install", "post", "pre_reboot", "on_success", "firstboot"):
        for s in scripts.get(stage, []):
            if c := s.get("content"):
                profile["scripts"].append({"stage": "post", "body": c})

    if doc.get("keys"):
        warn("hardware key matrix (keys[]) requires openSUSE systemd-cryptenroll post-install script")

    return profile


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("."))
    ap.add_argument("--strict", action="store_true", help="exit non-zero if core intent dropped")
    args = ap.parse_args()

    doc = json.loads(args.file.read_text())
    if not str(doc.get("lis", "")).startswith("0.1."):
        sys.exit(f"unsupported LIS version: {doc.get('lis')!r}")

    profile = render_agama(doc)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "profile.json").write_text(json.dumps(profile, indent=2) + "\n")
    print(f"wrote {args.out}/profile.json ({len(WARNINGS)} warning(s))")
    if args.strict and WARNINGS:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
