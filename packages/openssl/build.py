#!/usr/bin/env python3
"""Cross-compile static `openssl-3.4.1` (`libssl.a`, `libcrypto.a`, headers) for
`wasm64-submilli-linux-musl` -> `$WASMOS_DESTDIR/usr/{include,lib}`.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

SRC = pathlib.Path(os.environ["WASMOS_SRC"])
BUILD = pathlib.Path(os.environ["WASMOS_BUILD"])
DESTDIR = pathlib.Path(os.environ["WASMOS_DESTDIR"])
TOOLCHAIN = pathlib.Path(os.environ["WASMOS_TOOLCHAIN"])
SYSROOT = pathlib.Path(os.environ["WASMOS_SYSROOT"])
JOBS = os.environ.get("WASMOS_JOBS", str(os.cpu_count() or 4))
CROSS_BIN = TOOLCHAIN / "bin"
TRIPLE = "wasm64-submilli-linux-musl"


def main() -> None:
    if not (SYSROOT / "lib/libz.a").is_file():
        raise SystemExit(f"Missing {SYSROOT / 'lib/libz.a'}; install zlib build_dep first")

    BUILD.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.pop("CROSS_COMPILE", None)
    env["PATH"] = f"{CROSS_BIN}:{env.get('PATH', '')}"
    env["CC"] = str(CROSS_BIN / f"{TRIPLE}-clang")
    env["CXX"] = str(CROSS_BIN / f"{TRIPLE}-clang++")
    env["AR"] = str(CROSS_BIN / f"{TRIPLE}-ar")
    env["RANLIB"] = str(CROSS_BIN / f"{TRIPLE}-ranlib")
    env["NM"] = str(CROSS_BIN / f"{TRIPLE}-nm")

    configure_cmd = [
        "perl",
        str(SRC / "Configure"),
        "linux-generic64",
        "no-asm",
        "no-shared",
        "no-dso",
        "no-engine",
        "no-async",
        "no-afalgeng",
        "no-tests",
        "no-ui-console",
        "zlib",
        f"--with-zlib-include={SYSROOT / 'include'}",
        f"--with-zlib-lib={SYSROOT / 'lib'}",
        "--prefix=/usr",
        "--libdir=lib",
        "--openssldir=/etc/ssl",
        "-O2",
    ]
    subprocess.run(configure_cmd, cwd=BUILD, env=env, check=True)
    subprocess.run(["make", f"-j{JOBS}", "build_libs"], cwd=BUILD, env=env, check=True)
    subprocess.run(["make", f"DESTDIR={DESTDIR}", "install_dev"], cwd=BUILD, env=env, check=True)

    for p in DESTDIR.rglob("*"):
        if p.is_file() and not p.is_symlink():
            p.chmod(0o644)

    lib_ssl = DESTDIR / "usr/lib/libssl.a"
    print(f"Built OpenSSL 3.4.1 -> {lib_ssl} ({lib_ssl.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
