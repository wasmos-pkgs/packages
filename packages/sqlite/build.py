#!/usr/bin/env python3
"""Cross-compile thread-safe shared (`libsqlite3.so.0`) and static (`libsqlite3.a`)
`sqlite-3.49.1` for `wasm64-submilli-linux-musl` -> `$WASMOS_DESTDIR/usr/{include,lib}`.
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


def symlink_force(target: str, link: pathlib.Path) -> None:
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(target, link)


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    cc = str(CROSS_BIN / f"{TRIPLE}-clang")
    ar = str(CROSS_BIN / f"{TRIPLE}-ar")
    ranlib = str(CROSS_BIN / f"{TRIPLE}-ranlib")

    cflags = [
        "-O2",
        "-fPIC",
        "-DSQLITE_THREADSAFE=1",
        "-DSQLITE_ENABLE_FTS5=1",
        "-DSQLITE_ENABLE_JSON1=1",
        "-DSQLITE_ENABLE_RTREE=1",
        "-DSQLITE_ENABLE_MATH_FUNCTIONS=1",
    ]
    obj = BUILD / "sqlite3.o"
    lib_a = BUILD / "libsqlite3.a"
    subprocess.run([cc, *cflags, "-c", str(SRC / "sqlite3.c"), "-o", str(obj)], check=True)
    subprocess.run([ar, "rcs", str(lib_a), str(obj)], check=True)
    subprocess.run([ranlib, str(lib_a)], check=True)

    inc_dir = DESTDIR / "usr/include"
    lib_dir = DESTDIR / "usr/lib"
    pc_dir = lib_dir / "pkgconfig"
    inc_dir.mkdir(parents=True, exist_ok=True)
    pc_dir.mkdir(parents=True, exist_ok=True)

    so_real = lib_dir / "libsqlite3.so.0.8.6"
    subprocess.run(
        [
            cc,
            "-shared",
            "-Wl,-soname,libsqlite3.so.0",
            str(obj),
            "-lpthread",
            "-ldl",
            "-lm",
            "-lc",
            "-o",
            str(so_real),
        ],
        check=True,
    )
    symlink_force("libsqlite3.so.0.8.6", lib_dir / "libsqlite3.so.0")
    symlink_force("libsqlite3.so.0", lib_dir / "libsqlite3.so")

    shutil.copy2(lib_a, lib_dir / "libsqlite3.a")
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
        "Libs: -L${libdir} -lsqlite3\n"
        "Libs.private: -lpthread -ldl -lm\n"
        "Cflags: -I${includedir}\n",
        encoding="utf-8",
    )

    for p in DESTDIR.rglob("*"):
        if p.is_file() and not p.is_symlink():
            if p.read_bytes()[:4] == b"\x00asm":
                p.chmod(0o755)
            else:
                p.chmod(0o644)

    print(f"Built sqlite-3.49.1 -> {so_real} ({so_real.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
