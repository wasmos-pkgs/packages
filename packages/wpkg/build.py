#!/usr/bin/env python3
"""Cross-compile `wpkg` for `wasm64-submilli-linux-musl`
-> `$WASMOS_DESTDIR/usr/bin/wpkg`.
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


def main() -> None:
    manifest = SRC / "crates/wpkg/Cargo.toml"
    if not manifest.is_file():
        raise SystemExit(f"Missing wpkg manifest: {manifest}")

    target_dir = BUILD / "target"
    target_dir.mkdir(parents=True, exist_ok=True)

    cargo_wasmos = TOOLCHAIN / "rust/cargo-wasmos.py"
    cmd = [
        sys.executable,
        str(cargo_wasmos),
        "--toolchain",
        str(TOOLCHAIN),
        "--",
        "build",
        "--release",
        "--manifest-path",
        str(manifest),
        "--target-dir",
        str(target_dir),
    ]
    subprocess.run(cmd, check=True)

    built_bin = target_dir / TRIPLE / "release/wpkg"
    if not built_bin.is_file():
        raise SystemExit(f"Expected built binary not found: {built_bin}")

    out_bin = DESTDIR / "usr/bin/wpkg"
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
    print(f"Built wpkg -> {out_bin} ({out_bin.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
