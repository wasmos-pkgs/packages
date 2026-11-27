#!/usr/bin/env python3
"""Cross-compile static `sqlite-3.49.1` (`libsqlite3.a` + headers) for
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
CROSS_BIN = TOOLCHAIN / "bin"
TRIPLE = "wasm64-submilli-linux-musl"


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    cc = str(CROSS_BIN / f"{TRIPLE}-clang")
    ar = str(CROSS_BIN / f"{TRIPLE}-ar")
    ranlib = str(CROSS_BIN / f"{TRIPLE}-ranlib")

    cflags = [
        "-O2",
        "-DSQLITE_THREADSAFE=0",
        "-DSQLITE_OMIT_LOAD_EXTENSION=1",
        "-DSQLITE_ENABLE_FTS5=1",
        "-DSQLITE_ENABLE_JSON1=1",
        "-DSQLITE_ENABLE_RTREE=1",
        "-DSQLITE_ENABLE_MATH_FUNCTIONS=1",
    ]
    obj = BUILD / "sqlite3.o"
    lib = BUILD / "libsqlite3.a"
    subprocess.run([cc, *cflags, "-c", str(SRC / "sqlite3.c"), "-o", str(obj)], check=True)
    subprocess.run([ar, "rcs", str(lib), str(obj)], check=True)
    subprocess.run([ranlib, str(lib)], check=True)

    inc_dir = DESTDIR / "usr/include"
    lib_dir = DESTDIR / "usr/lib"
    pc_dir = lib_dir / "pkgconfig"
    inc_dir.mkdir(parents=True, exist_ok=True)
    pc_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(lib, lib_dir / "libsqlite3.a")
    shutil.copy2(SRC / "sqlite3.h", inc_dir / "sqlite3.h")
    shutil.copy2(SRC / "sqlite3ext.h", inc_dir / "sqlite3ext.h")
    (pc_dir / "sqlite3.pc").write_text(
        "prefix=/usr\n"
        "exec_prefix=${prefix}\n"
        "libdir=${exec_prefix}/lib\n"
        "includedir=${prefix}/include\n\n"
        "Name: SQLite\n"
        "Description: SQL database engine\n"
        "Version: 3.49.1\n"
        "Libs: -L${libdir} -lsqlite3 -lm\n"
        "Cflags: -I${includedir}\n",
        encoding="utf-8",
    )

    for p in DESTDIR.rglob("*"):
        if p.is_file() and not p.is_symlink():
            p.chmod(0o644)

    print(f"Built sqlite-3.49.1 -> {lib_dir / 'libsqlite3.a'} ({(lib_dir / 'libsqlite3.a').stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
