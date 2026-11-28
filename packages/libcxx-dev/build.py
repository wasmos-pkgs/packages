#!/usr/bin/env python3
"""Package `libcxx-dev` (`libc++`, `libc++abi`, and Wasm EH unwinder) headers
and static libraries from `$WASMOS_SYSROOT` into `$WASMOS_DESTDIR/usr/{include,lib}`.
"""

from __future__ import annotations

import os
import pathlib
import shutil

DESTDIR = pathlib.Path(os.environ["WASMOS_DESTDIR"])
SYSROOT = pathlib.Path(os.environ["WASMOS_SYSROOT"])

LIBCXX_LIBS = (
    "libc++.a",
    "libc++abi.a",
    "libc++experimental.a",
)


def main() -> None:
    src_cxx_inc = SYSROOT / "include/c++"
    src_lib = SYSROOT / "lib"
    if not src_cxx_inc.is_dir() or not (src_lib / "libc++.a").is_file():
        raise SystemExit(f"Missing libcxx headers/libraries in {SYSROOT}")

    dst_cxx_inc = DESTDIR / "usr/include/c++"
    dst_lib = DESTDIR / "usr/lib"
    dst_cxx_inc.parent.mkdir(parents=True, exist_ok=True)
    dst_lib.mkdir(parents=True, exist_ok=True)

    shutil.copytree(src_cxx_inc, dst_cxx_inc, symlinks=True, dirs_exist_ok=True)

    copied = 0
    for name in LIBCXX_LIBS:
        src = src_lib / name
        if src.is_file():
            dst = dst_lib / name
            shutil.copy2(src, dst)
            dst.chmod(0o644)
            copied += 1

    if copied < 2:
        raise SystemExit(f"Expected libc++.a and libc++abi.a in {src_lib}")

    print(f"Packaged libcxx-dev -> {dst_cxx_inc} and {copied} static libs in {dst_lib}", flush=True)


if __name__ == "__main__":
    main()
