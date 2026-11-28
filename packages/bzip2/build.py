#!/usr/bin/env python3
"""Cross-compile shared (`libbz2.so.1.0`) and static (`libbz2.a`) `bzip2-1.0.8` for
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

BZ2_SRCS = [
    "blocksort.c",
    "huffman.c",
    "crctable.c",
    "randtable.c",
    "compress.c",
    "decompress.c",
    "bzlib.c",
]


def symlink_force(target: str, link: pathlib.Path) -> None:
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(target, link)


def main() -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    shutil.copytree(SRC, BUILD)

    cc = str(CROSS_BIN / f"{TRIPLE}-clang")
    ar = str(CROSS_BIN / f"{TRIPLE}-ar")
    ranlib = str(CROSS_BIN / f"{TRIPLE}-ranlib")

    objs: list[str] = []
    for c_name in BZ2_SRCS:
        obj = BUILD / f"{c_name[:-2]}.o"
        subprocess.run(
            [cc, "-O2", "-fPIC", "-D_FILE_OFFSET_BITS=64", "-c", str(BUILD / c_name), "-o", str(obj)],
            check=True,
        )
        objs.append(str(obj))

    inc_dir = DESTDIR / "usr/include"
    lib_dir = DESTDIR / "usr/lib"
    inc_dir.mkdir(parents=True, exist_ok=True)
    lib_dir.mkdir(parents=True, exist_ok=True)

    lib_a = lib_dir / "libbz2.a"
    subprocess.run([ar, "rcs", str(lib_a), *objs], check=True)
    subprocess.run([ranlib, str(lib_a)], check=True)

    so_real = lib_dir / "libbz2.so.1.0.8"
    subprocess.run(
        [cc, "-shared", "-Wl,-soname,libbz2.so.1.0", *objs, "-lc", "-o", str(so_real)],
        check=True,
    )
    symlink_force("libbz2.so.1.0.8", lib_dir / "libbz2.so.1.0")
    symlink_force("libbz2.so.1.0.8", lib_dir / "libbz2.so.1")
    symlink_force("libbz2.so.1.0", lib_dir / "libbz2.so")

    shutil.copy2(BUILD / "bzlib.h", inc_dir / "bzlib.h")
    (inc_dir / "bzlib.h").chmod(0o644)
    lib_a.chmod(0o644)
    so_real.chmod(0o755)
    print(f"Built bzip2-1.0.8 -> {so_real} ({so_real.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
