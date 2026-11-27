#!/usr/bin/env python3
"""Cross-compile static `zlib-1.3.1` for `wasm64-submilli-linux-musl`
-> `$WASMOS_DESTDIR/usr/{include,lib}`.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

SRC = pathlib.Path(os.environ["WASMOS_SRC"])
BUILD = pathlib.Path(os.environ["WASMOS_BUILD"])
DESTDIR = pathlib.Path(os.environ["WASMOS_DESTDIR"])
TOOLCHAIN = pathlib.Path(os.environ["WASMOS_TOOLCHAIN"])
JOBS = os.environ.get("WASMOS_JOBS", str(os.cpu_count() or 4))
CROSS_BIN = TOOLCHAIN / "bin"
TRIPLE = "wasm64-submilli-linux-musl"


def main() -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    shutil.copytree(SRC, BUILD)

    env = os.environ.copy()
    env["PATH"] = f"{CROSS_BIN}:{env.get('PATH', '')}"
    env["CC"] = str(CROSS_BIN / f"{TRIPLE}-clang")
    env["AR"] = str(CROSS_BIN / f"{TRIPLE}-ar")
    env["RANLIB"] = str(CROSS_BIN / f"{TRIPLE}-ranlib")
    env["CFLAGS"] = "-O2"
    # Ensure macOS hosts use `$AR rc` rather than Apple `libtool`.
    env["uname"] = "Linux"

    subprocess.run(
        ["sh", "./configure", "--static", "--prefix=/usr", "--libdir=/usr/lib", "--includedir=/usr/include"],
        cwd=BUILD,
        env=env,
        check=True,
    )
    subprocess.run(["make", f"-j{JOBS}", "libz.a"], cwd=BUILD, env=env, check=True)
    subprocess.run(["make", f"DESTDIR={DESTDIR}", "install"], cwd=BUILD, env=env, check=True)

    for p in DESTDIR.rglob("*"):
        if p.is_file() and not p.is_symlink():
            p.chmod(0o644)

    lib_path = DESTDIR / "usr/lib/libz.a"
    print(f"Built zlib-1.3.1 -> {lib_path} ({lib_path.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
