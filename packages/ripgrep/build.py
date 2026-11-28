#!/usr/bin/env python3
"""Cross-compile `ripgrep` (`rg`) 16.0.0 for `wasm64-submilli-linux-musl`
-> `$WASMOS_DESTDIR/usr/bin/rg`.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

SRC = pathlib.Path(os.environ["WASMOS_SRC"])
BUILD = pathlib.Path(os.environ["WASMOS_BUILD"])
DESTDIR = pathlib.Path(os.environ["WASMOS_DESTDIR"])
TOOLCHAIN = pathlib.Path(os.environ["WASMOS_TOOLCHAIN"])
CROSS_BIN = TOOLCHAIN / "bin"
TRIPLE = "wasm64-submilli-linux-musl"


def disable_jemalloc_on_wasm64(src: pathlib.Path) -> None:
    """Disable tikv-jemallocator on wasm64 (jemalloc has no wasm64 support)."""
    toml = src / "Cargo.toml"
    old_toml = '[target.\'cfg(all(target_env = "musl", target_pointer_width = "64"))\'.dependencies.tikv-jemallocator]'
    new_toml = '[target.\'cfg(all(target_env = "musl", target_pointer_width = "64", not(target_arch = "wasm64")))\'.dependencies.tikv-jemallocator]'
    txt = toml.read_text(encoding="utf-8")
    if new_toml not in txt:
        if old_toml not in txt:
            raise SystemExit(f"Expected tikv-jemallocator cfg block not found in {toml}")
        toml.write_text(txt.replace(old_toml, new_toml, 1), encoding="utf-8")

    main_rs = src / "crates/core/main.rs"
    old_rs = '#[cfg(all(target_env = "musl", target_pointer_width = "64"))]'
    new_rs = '#[cfg(all(target_env = "musl", target_pointer_width = "64", not(target_arch = "wasm64")))]'
    mtxt = main_rs.read_text(encoding="utf-8")
    if new_rs not in mtxt:
        if old_rs not in mtxt:
            raise SystemExit(f"Expected tikv-jemallocator cfg attribute not found in {main_rs}")
        main_rs.write_text(mtxt.replace(old_rs, new_rs, 1), encoding="utf-8")


def main() -> None:
    disable_jemalloc_on_wasm64(SRC)

    target_dir = BUILD / "target"
    target_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["CARGO_PROFILE_RELEASE_OPT_LEVEL"] = "s"
    env["CARGO_PROFILE_RELEASE_LTO"] = "thin"

    cargo_wasmos = pathlib.Path(__file__).resolve().parents[2] / "scripts/cargo-wasmos.py"
    cmd = [
        sys.executable,
        str(cargo_wasmos),
        "--toolchain",
        str(TOOLCHAIN),
        "--",
        "build",
        "--release",
        "--ignore-rust-version",
        "--manifest-path",
        str(SRC / "Cargo.toml"),
        "--target-dir",
        str(target_dir),
    ]
    subprocess.run(cmd, env=env, check=True)

    built_bin = target_dir / TRIPLE / "release/rg"
    if not built_bin.is_file():
        raise SystemExit(f"Expected built binary not found: {built_bin}")

    out_bin = DESTDIR / "usr/bin/rg"
    out_bin.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built_bin, out_bin)
    subprocess.run(
        [
            str(CROSS_BIN / f"{TRIPLE}-strip"),
            "--strip-debug",
            "--keep-section=submilli.target",
            str(out_bin),
        ],
        check=True,
    )
    out_bin.chmod(0o755)
    print(f"Built ripgrep-16.0.0 -> {out_bin} ({out_bin.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
