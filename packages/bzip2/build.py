#!/usr/bin/env python3
"""Cross-compile static `bzip2-1.0.8` (`libbz2.a` + `bzlib.h`) for
`wasm64-submilli-linux-musl` -> `$WASMOS_DESTDIR/usr/{include,lib}`.
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

    cc = str(CROSS_BIN / f"{TRIPLE}-clang")
    ar = str(CROSS_BIN / f"{TRIPLE}-ar")
    ranlib = str(CROSS_BIN / f"{TRIPLE}-ranlib")

    subprocess.run(
        [
            "make",
            f"-j{JOBS}",
            f"CC={cc}",
            f"AR={ar}",
            f"RANLIB={ranlib}",
            "CFLAGS=-O2 -D_FILE_OFFSET_BITS=64",
            "libbz2.a",
        ],
        cwd=BUILD,
        check=True,
    )

    inc_dir = DESTDIR / "usr/include"
    lib_dir = DESTDIR / "usr/lib"
    inc_dir.mkdir(parents=True, exist_ok=True)
    lib_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(BUILD / "bzlib.h", inc_dir / "bzlib.h")
    shutil.copy2(BUILD / "libbz2.a", lib_dir / "libbz2.a")
    (inc_dir / "bzlib.h").chmod(0o644)
    (lib_dir / "libbz2.a").chmod(0o644)
    print(f"Built bzip2-1.0.8 -> {lib_dir / 'libbz2.a'} ({(lib_dir / 'libbz2.a').stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
