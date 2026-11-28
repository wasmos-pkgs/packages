#!/usr/bin/env python3
"""Cross-compile shared (`libz.so.1`) and static (`libz.a`) `zlib-1.3.1` for
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

ZLIB_SRCS = [
    "adler32.c",
    "compress.c",
    "crc32.c",
    "deflate.c",
    "gzclose.c",
    "gzlib.c",
    "gzread.c",
    "gzwrite.c",
    "infback.c",
    "inffast.c",
    "inflate.c",
    "inftrees.c",
    "trees.c",
    "uncompr.c",
    "zutil.c",
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

    env = os.environ.copy()
    env["PATH"] = f"{CROSS_BIN}:{env.get('PATH', '')}"
    env["CC"] = cc
    env["AR"] = ar
    env["RANLIB"] = ranlib
    env["CFLAGS"] = "-O2 -fPIC"
    env["uname"] = "Linux"

    subprocess.run(
        ["sh", "./configure", "--static", "--prefix=/usr", "--libdir=/usr/lib", "--includedir=/usr/include"],
        cwd=BUILD,
        env=env,
        check=True,
    )
    subprocess.run(["make", f"-j{JOBS}", "libz.a"], cwd=BUILD, env=env, check=True)
    subprocess.run(["make", f"DESTDIR={DESTDIR}", "install"], cwd=BUILD, env=env, check=True)

    pic_objs: list[str] = []
    for c_name in ZLIB_SRCS:
        obj = BUILD / f"{c_name[:-2]}.lo"
        subprocess.run(
            [cc, "-O2", "-fPIC", "-D_LARGEFILE64_SOURCE=1", "-c", str(BUILD / c_name), "-o", str(obj)],
            check=True,
        )
        pic_objs.append(str(obj))

    lib_dir = DESTDIR / "usr/lib"
    so_real = lib_dir / "libz.so.1.3.1"
    subprocess.run(
        [cc, "-shared", "-Wl,-soname,libz.so.1", *pic_objs, "-lc", "-o", str(so_real)],
        check=True,
    )
    symlink_force("libz.so.1.3.1", lib_dir / "libz.so.1")
    symlink_force("libz.so.1", lib_dir / "libz.so")

    for p in DESTDIR.rglob("*"):
        if p.is_file() and not p.is_symlink():
            if p.read_bytes()[:4] == b"\x00asm":
                p.chmod(0o755)
            else:
                p.chmod(0o644)

    print(f"Built zlib-1.3.1 -> {so_real} ({so_real.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
