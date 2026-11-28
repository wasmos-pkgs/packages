#!/usr/bin/env python3
"""Package `musl-dev` headers, CRT objects, static C libraries, and compiler-rt
builtins from `$WASMOS_SYSROOT` into `$WASMOS_DESTDIR/usr/{include,lib}`.
"""

from __future__ import annotations

import os
import pathlib
import shutil

DESTDIR = pathlib.Path(os.environ["WASMOS_DESTDIR"])
SYSROOT = pathlib.Path(os.environ["WASMOS_SYSROOT"])

MUSL_STATIC_FILES = (
    "crt1.o",
    "Scrt1.o",
    "rcrt1.o",
    "crti.o",
    "crtn.o",
    "libc.a",
    "libm.a",
    "libpthread.a",
    "librt.a",
    "libdl.a",
    "libutil.a",
    "libxnet.a",
    "libcrypt.a",
    "libresolv.a",
    "libclang_rt.builtins.a",
    "libclang_rt.builtins-wasm64.a",
)


def main() -> None:
    src_inc = SYSROOT / "include"
    src_lib = SYSROOT / "lib"
    if not src_inc.is_dir() or not (src_lib / "libc.a").is_file():
        raise SystemExit(f"Incomplete musl sysroot at {SYSROOT}")

    dst_inc = DESTDIR / "usr/include"
    dst_lib = DESTDIR / "usr/lib"
    dst_inc.mkdir(parents=True, exist_ok=True)
    dst_lib.mkdir(parents=True, exist_ok=True)

    # Copy C + Linux UAPI headers (excluding C++ headers owned by `libcxx-dev`)
    for entry in sorted(src_inc.iterdir()):
        if entry.name == "c++":
            continue
        dst = dst_inc / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dst, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, dst)

    copied_libs = 0
    for name in MUSL_STATIC_FILES:
        src = src_lib / name
        if src.is_file():
            dst = dst_lib / name
            shutil.copy2(src, dst)
            dst.chmod(0o644)
            copied_libs += 1

    if copied_libs < 4:
        raise SystemExit(f"Expected musl static libraries in {src_lib}, only copied {copied_libs}")

    print(f"Packaged musl-dev -> {dst_inc} and {copied_libs} static libs in {dst_lib}", flush=True)


if __name__ == "__main__":
    main()
