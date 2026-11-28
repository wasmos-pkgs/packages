#!/usr/bin/env python3
"""Cross-compile Rust crates for `wasm64-submilli-linux-musl` using our built-in
patched `rustc` + bundled `cargo` from `wasmos-pkgs/toolchain`.

Example:
  python3 scripts/cargo-wasmos.py --toolchain /tmp/wasmos-toolchain -- build --release --manifest-path Cargo.toml
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess

TRIPLE = "wasm64-submilli-linux-musl"


def detect_locked_libc_version(cargo_args: list[str]) -> str | None:
    manifest_dir = pathlib.Path.cwd()
    for i, tok in enumerate(cargo_args):
        if tok == "--manifest-path" and i + 1 < len(cargo_args):
            manifest_dir = pathlib.Path(cargo_args[i + 1]).resolve().parent
            break
        if tok.startswith("--manifest-path="):
            manifest_dir = pathlib.Path(tok.split("=", 1)[1]).resolve().parent
            break
    for parent in [manifest_dir, *manifest_dir.parents]:
        lock_file = parent / "Cargo.lock"
        if lock_file.is_file():
            text = lock_file.read_text(encoding="utf-8", errors="ignore")
            m = re.search(
                r'\[\[package\]\]\s*\nname\s*=\s*"libc"\s*\nversion\s*=\s*"(0\.2\.\d+)"',
                text,
            )
            if m:
                return m.group(1)
            return None
    return None


def prepare_libc_override(base_libc: pathlib.Path, locked_version: str | None) -> pathlib.Path:
    if not locked_version:
        return base_libc
    cargo_toml = base_libc / "Cargo.toml"
    m = re.search(r'^version\s*=\s*"([^"]+)"', cargo_toml.read_text(encoding="utf-8"), flags=re.MULTILINE)
    base_ver = m.group(1) if m else ""
    if base_ver == locked_version:
        return base_libc

    override_dir = base_libc.parent / "libc-overrides" / f"libc-{locked_version}"
    if override_dir.is_dir():
        return override_dir

    override_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = override_dir.with_name(f".libc-{locked_version}.tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    shutil.copytree(base_libc, tmp_dir)
    t_toml = tmp_dir / "Cargo.toml"
    t_text = t_toml.read_text(encoding="utf-8")
    t_toml.write_text(
        re.sub(
            r'^version\s*=\s*"[^"]+"',
            f'version = "{locked_version}"',
            t_text,
            count=1,
            flags=re.MULTILINE,
        ),
        encoding="utf-8",
    )
    tmp_dir.replace(override_dir)
    return override_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-compile Rust crates for wasm64-submilli-linux-musl")
    parser.add_argument(
        "--toolchain",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("WASMOS_TOOLCHAIN", "")),
        help="Path to assembled WasmOS toolchain (or $WASMOS_TOOLCHAIN)",
    )
    parser.add_argument(
        "--linker",
        type=pathlib.Path,
        default=None,
        help="Override linker executable (defaults to <toolchain>/bin/wasm64-submilli-linux-musl-rust-linker)",
    )
    parser.add_argument("cargo_args", nargs=argparse.REMAINDER, help="Arguments passed to cargo (after `--`)")
    args = parser.parse_args()

    toolchain = args.toolchain.resolve()
    rustc_bin = toolchain / "rustc/bin/rustc"
    cargo_bin = toolchain / "rustc/bin/cargo"
    base_libc = toolchain / "rustc/share/wasmos/libc"
    for req in (toolchain / "sysroot/lib/libc.a", rustc_bin, cargo_bin, base_libc / "Cargo.toml"):
        if not req.is_file():
            raise SystemExit(f"Invalid --toolchain '{toolchain}' (missing {req})")

    cargo_args = list(args.cargo_args)
    if cargo_args and cargo_args[0] == "--":
        cargo_args = cargo_args[1:]
    if not cargo_args:
        cargo_args = ["build", "--release"]

    locked_libc = detect_locked_libc_version(cargo_args)
    libc_dir = prepare_libc_override(base_libc, locked_libc)
    linker = args.linker.resolve() if args.linker else (toolchain / f"bin/{TRIPLE}-rust-linker")

    env = os.environ.copy()
    env["PATH"] = f"{toolchain / 'bin'}:{toolchain / 'rustc/bin'}:{env.get('PATH', '')}"
    env["RUSTC"] = str(rustc_bin)
    env["CC_wasm64_submilli_linux_musl"] = str(toolchain / f"bin/{TRIPLE}-clang")
    env["CXX_wasm64_submilli_linux_musl"] = str(toolchain / f"bin/{TRIPLE}-clang++")
    env["AR_wasm64_submilli_linux_musl"] = str(toolchain / f"bin/{TRIPLE}-ar")
    env["RANLIB_wasm64_submilli_linux_musl"] = str(toolchain / f"bin/{TRIPLE}-ranlib")
    env["CARGO_TARGET_WASM64_SUBMILLI_LINUX_MUSL_LINKER"] = str(linker)

    extra_rustflags = env.get("RUSTFLAGS", "").strip()
    rustflags = "--cfg=rustix_use_libc"
    if extra_rustflags:
        rustflags = f"{rustflags} {extra_rustflags}"
    env["RUSTFLAGS"] = rustflags

    cmd = [
        str(cargo_bin),
        cargo_args[0],
        "--target",
        TRIPLE,
        "--config",
        f'patch.crates-io.libc.path="{libc_dir}"',
        *cargo_args[1:],
    ]
    print(f"+ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, env=env, check=True)


if __name__ == "__main__":
    main()
