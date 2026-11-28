//! CLI wrapper for `wasmos-postlink`, invoked automatically by `clang` after `wasm-ld`.

use std::env;
use std::fs;
use std::io::{self, Write};
use std::path::PathBuf;
use std::process::ExitCode;

use wasmos_postlink::rewrite_module_with_paths;

fn run() -> Result<(), String> {
    let mut args = env::args().skip(1);
    let mut input: Option<String> = None;
    let mut output: Option<String> = None;
    let mut lib_dirs: Vec<PathBuf> = Vec::new();
    let mut verbose = false;

    while let Some(arg) = args.next() {
        match arg.as_str() {
            "-o" => {
                output = Some(
                    args.next()
                        .ok_or_else(|| "missing path after -o".to_string())?,
                );
            }
            "-L" => {
                let dir = args
                    .next()
                    .ok_or_else(|| "missing path after -L".to_string())?;
                lib_dirs.push(PathBuf::from(dir));
            }
            s if s.starts_with("-L") && s.len() > 2 => {
                lib_dirs.push(PathBuf::from(s.get(2..).unwrap_or_default()));
            }
            "-v" | "--verbose" => verbose = true,
            other if !other.starts_with('-') && input.is_none() => {
                input = Some(other.to_string());
            }
            other => return Err(format!("unknown argument: {other}")),
        }
    }

    let in_path = input.ok_or_else(|| {
        "usage: wasmos-postlink [-L <dir>]... <input.wasm> [-o <output.wasm>] [-v]".to_string()
    })?;
    let out_path = output.unwrap_or_else(|| in_path.clone());

    let bytes = fs::read(&in_path).map_err(|e| format!("failed to read {in_path}: {e}"))?;
    let (rewritten, stats) =
        rewrite_module_with_paths(&bytes, &lib_dirs).map_err(|e| format!("{in_path}: {e}"))?;

    if verbose {
        let _ = writeln!(
            io::stderr(),
            "wasmos-postlink: {}: {}/{} defined functions fork-reachable (instrumented={})",
            in_path,
            stats.fork_reachable_functions,
            stats.defined_functions,
            stats.instrumented
        );
    }

    fs::write(&out_path, rewritten).map_err(|e| format!("failed to write {out_path}: {e}"))?;
    Ok(())
}

fn main() -> ExitCode {
    if let Err(err) = run() {
        let _ = writeln!(io::stderr(), "wasmos-postlink: error: {err}");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}
