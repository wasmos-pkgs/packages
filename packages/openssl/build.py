#!/usr/bin/env python3
"""Cross-compile shared (`libssl.so.3`, `libcrypto.so.3`) and static
`openssl-3.4.1` for `wasm64-submilli-linux-musl` -> `$WASMOS_DESTDIR/usr/{include,lib}`.
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
    if not (SYSROOT / "lib/libz.so").exists():
        raise SystemExit(f"Missing {SYSROOT / 'lib/libz.so'}; install zlib build_dep first")

    BUILD.mkdir(parents=True, exist_ok=True)
    strip_bin = str(CROSS_BIN / f"{TRIPLE}-strip")
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
        "shared",
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
        "-fPIC",
        f"-L{SYSROOT / 'lib'}",
    ]
    subprocess.run(configure_cmd, cwd=BUILD, env=env, check=True)
    subprocess.run(["make", f"-j{JOBS}", "build_libs"], cwd=BUILD, env=env, check=True)
    subprocess.run(["make", f"DESTDIR={DESTDIR}", "install_dev"], cwd=BUILD, env=env, check=True)

    lib_ssl = DESTDIR / "usr/lib/libssl.so.3"
    lib_crypto = DESTDIR / "usr/lib/libcrypto.so.3"
    if not lib_ssl.is_file() or not lib_crypto.is_file():
        raise SystemExit("Expected OpenSSL shared libraries libssl.so.3 and libcrypto.so.3 were not built")

    for p in DESTDIR.rglob("*"):
        if p.is_file() and not p.is_symlink():
            if p.read_bytes()[:4] == b"\x00asm":
                subprocess.run(
                    [
                        strip_bin,
                        "--strip-debug",
                        "--keep-section=dylink.0",
                        "--keep-section=submilli.target",
                        "--keep-section=submilli.fork_exports",
                        "--keep-section=name",
                        str(p),
                    ],
                    check=True,
                )
                p.chmod(0o755)
            else:
                p.chmod(0o644)

    print(
        f"Built OpenSSL 3.4.1 -> {lib_ssl} ({lib_ssl.stat().st_size} bytes), "
        f"{lib_crypto} ({lib_crypto.stat().st_size} bytes)",
        flush=True,
    )


if __name__ == "__main__":
    main()
