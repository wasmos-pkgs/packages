#!/usr/bin/env python3
"""Package `musl-1.2.5` dynamic runtime (`libc.so` + `ld-musl-wasm64.so.1`) from
the toolchain sysroot into `$WASMOS_DESTDIR/{lib,usr/lib}`.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

DESTDIR = pathlib.Path(os.environ["WASMOS_DESTDIR"])
TOOLCHAIN = pathlib.Path(os.environ["WASMOS_TOOLCHAIN"])
SYSROOT = pathlib.Path(os.environ["WASMOS_SYSROOT"])
CROSS_BIN = TOOLCHAIN / "bin"
TRIPLE = "wasm64-submilli-linux-musl"


def symlink_force(target: str, link: pathlib.Path) -> None:
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(target, link)


def main() -> None:
    src_libc = SYSROOT / "lib/libc.so"
    if not src_libc.is_file():
        raise SystemExit(f"Missing {src_libc} in toolchain sysroot")

    lib_dir = DESTDIR / "lib"
    usr_lib_dir = DESTDIR / "usr/lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    usr_lib_dir.mkdir(parents=True, exist_ok=True)

    dst_libc = lib_dir / "libc.so"
    shutil.copy2(src_libc, dst_libc)
    subprocess.run(
        [
            str(CROSS_BIN / f"{TRIPLE}-strip"),
            "--strip-debug",
            "--keep-section=dylink.0",
            "--keep-section=submilli.target",
            "--keep-section=submilli.fork_exports",
            "--keep-section=name",
            str(dst_libc),
        ],
        check=True,
    )
    dst_libc.chmod(0o755)

    symlink_force("libc.so", lib_dir / "ld-musl-wasm64.so.1")
    symlink_force("/lib/libc.so", usr_lib_dir / "libc.so")
    print(f"Packaged musl-1.2.5 -> {dst_libc} ({dst_libc.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
