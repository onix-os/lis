//! lis2nixos — the default LIS → NixOS translator.
//!
//! Usage: lis2nixos FILE.lis.json [--out DIR] [--strict]
//! Writes disko.nix, hardware.nix, and configuration.nix into DIR.

use std::path::PathBuf;
use std::process::ExitCode;

fn main() -> ExitCode {
    let mut args = std::env::args().skip(1);
    let mut file = None;
    let mut out = PathBuf::from(".");
    let mut strict = false;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--out" => match args.next() {
                Some(dir) => out = PathBuf::from(dir),
                None => return usage("--out needs a directory"),
            },
            "--strict" => strict = true,
            "--help" | "-h" => return usage(""),
            _ if file.is_none() => file = Some(PathBuf::from(arg)),
            other => return usage(&format!("unexpected argument {other:?}")),
        }
    }
    let Some(file) = file else {
        return usage("missing input file");
    };

    let run = || -> Result<Vec<String>, String> {
        let text = std::fs::read_to_string(&file)
            .map_err(|err| format!("failed to read {}: {err}", file.display()))?;
        let doc = lis::Document::from_json_validated(&text)?;
        let output = lis::nixos::translate(&doc)?;
        std::fs::create_dir_all(&out)
            .map_err(|err| format!("failed to create {}: {err}", out.display()))?;
        for (name, content) in [
            ("disko.nix", &output.disko),
            ("hardware.nix", &output.hardware),
            ("configuration.nix", &output.configuration),
        ] {
            std::fs::write(out.join(name), content)
                .map_err(|err| format!("failed to write {name}: {err}"))?;
        }
        Ok(output.warnings)
    };

    match run() {
        Ok(warnings) => {
            for warning in &warnings {
                eprintln!("warning: {warning}");
            }
            println!(
                "wrote {}/disko.nix, hardware.nix, configuration.nix ({} warning(s))",
                out.display(),
                warnings.len()
            );
            if strict && !warnings.is_empty() {
                return ExitCode::from(1);
            }
            ExitCode::SUCCESS
        }
        Err(err) => {
            eprintln!("error: {err}");
            ExitCode::from(1)
        }
    }
}

fn usage(err: &str) -> ExitCode {
    if !err.is_empty() {
        eprintln!("error: {err}");
    }
    eprintln!("usage: lis2nixos FILE.lis.json [--out DIR] [--strict]");
    ExitCode::from(if err.is_empty() { 0 } else { 2 })
}
