#!/usr/bin/env python3
"""Build Rust crates for `wasm64-submilli-linux-musl` using stock `rustc` + patched `rust-src`.

Example:
  python3 cargo-wasmos.py --toolchain /path/to/toolchain -- build --release --manifest-path Cargo.toml
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys

RUST_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RUST_DIR))
import patch_rust_src  # noqa: E402

TRIPLE = "wasm64-submilli-linux-musl"
TARGET_JSON = RUST_DIR / f"{TRIPLE}.json"


def ensure_rust_linker(toolchain: pathlib.Path) -> pathlib.Path:
    linker = toolchain / f"bin/{TRIPLE}-rust-linker"
    if linker.is_file():
        return linker
    linker.parent.mkdir(parents=True, exist_ok=True)
    linker.write_text(
        f"""#!/bin/sh
DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
exec "$DIR/llvm/bin/clang" -static "$@" --target={TRIPLE} --sysroot="$DIR/sysroot" "$DIR/sysroot/lib/libclang_rt.builtins-wasm64.a"
""",
        encoding="utf-8",
    )
    linker.chmod(0o755)
    return linker


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-compile Rust crates for wasm64-submilli-linux-musl")
    parser.add_argument(
        "--toolchain",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("WASMOS_TOOLCHAIN", "")),
        help="Path to extracted wasm64-submilli-linux-musl toolchain (or $WASMOS_TOOLCHAIN)",
    )
    parser.add_argument(
        "--sysroot-cache",
        type=pathlib.Path,
        default=None,
        help="Directory to cache the patched Rust sysroot (defaults to <toolchain>/rust-sysroot)",
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
    if not (toolchain / "sysroot/lib/libc.a").is_file():
        raise SystemExit(f"Invalid --toolchain '{toolchain}' (missing sysroot/lib/libc.a)")

    cargo_args = list(args.cargo_args)
    if cargo_args and cargo_args[0] == "--":
        cargo_args = cargo_args[1:]
    if not cargo_args:
        cargo_args = ["build", "--release"]

    sysroot_cache = args.sysroot_cache or (toolchain / "rust-sysroot")
    patched_sysroot, libc_dir = patch_rust_src.prepare_patched_sysroot(sysroot_cache)
    linker = args.linker.resolve() if args.linker else ensure_rust_linker(toolchain)

    env = os.environ.copy()
    env["PATH"] = f"{toolchain / 'bin'}:{env.get('PATH', '')}"
    env["RUSTC_BOOTSTRAP"] = "1"
    env["__CARGO_TESTS_ONLY_SRC_ROOT"] = str(patched_sysroot / "lib/rustlib/src/rust/library")
    env["CC_wasm64_submilli_linux_musl"] = str(toolchain / f"bin/{TRIPLE}-clang")
    env["CXX_wasm64_submilli_linux_musl"] = str(toolchain / f"bin/{TRIPLE}-clang++")
    env["AR_wasm64_submilli_linux_musl"] = str(toolchain / f"bin/{TRIPLE}-ar")
    env["RANLIB_wasm64_submilli_linux_musl"] = str(toolchain / f"bin/{TRIPLE}-ranlib")

    extra_rustflags = env.get("RUSTFLAGS", "").strip()
    rustflags = f"--sysroot={patched_sysroot} -C linker={linker}"
    if extra_rustflags:
        rustflags = f"{rustflags} {extra_rustflags}"
    env["RUSTFLAGS"] = rustflags

    cmd = [
        "cargo",
        cargo_args[0],
        "--target", str(TARGET_JSON),
        "--config", f'patch.crates-io.libc.path="{libc_dir}"',
        "-Z", "build-std=core,alloc,std,panic_abort",
        "-Z", "json-target-spec",
        *cargo_args[1:],
    ]
    print(f"+ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, env=env, check=True)


if __name__ == "__main__":
    main()
