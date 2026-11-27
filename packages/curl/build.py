#!/usr/bin/env python3
"""Cross-compile static `curl-8.12.1` for `wasm64-submilli-linux-musl`
-> `$WASMOS_DESTDIR/usr/bin/curl`.
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
SYSROOT = pathlib.Path(os.environ["WASMOS_SYSROOT"])
JOBS = os.environ.get("WASMOS_JOBS", str(os.cpu_count() or 4))
CROSS_BIN = TOOLCHAIN / "bin"
TRIPLE = "wasm64-submilli-linux-musl"


def main() -> None:
    if not (SYSROOT / "lib/libssl.a").is_file():
        raise SystemExit(f"Missing {SYSROOT / 'lib/libssl.a'}; install openssl build_dep first")

    config_sub = SRC / "config.sub"
    if config_sub.is_file():
        text = config_sub.read_text(encoding="utf-8", errors="replace")
        if "wasm64" not in text:
            config_sub.write_text(text.replace("wasm32", "wasm32 | wasm64"), encoding="utf-8")

    BUILD.mkdir(parents=True, exist_ok=True)
    build_triplet = subprocess.check_output(["sh", str(SRC / "config.guess")], text=True).strip()

    env = os.environ.copy()
    env["PATH"] = f"{CROSS_BIN}:{env.get('PATH', '')}"
    env["CC"] = str(CROSS_BIN / f"{TRIPLE}-clang")
    env["AR"] = str(CROSS_BIN / f"{TRIPLE}-ar")
    env["RANLIB"] = str(CROSS_BIN / f"{TRIPLE}-ranlib")
    env["STRIP"] = str(CROSS_BIN / f"{TRIPLE}-strip")
    env["PKG_CONFIG_LIBDIR"] = str(SYSROOT / "lib/pkgconfig")
    env["PKG_CONFIG_PATH"] = str(SYSROOT / "lib/pkgconfig")
    env["CFLAGS"] = "-O2"
    env["CPPFLAGS"] = f"-I{SYSROOT / 'include'}"
    env["LDFLAGS"] = f"-static -L{SYSROOT / 'lib'}"
    env["LIBS"] = "-lssl -lcrypto -lz"

    configure_cmd = [
        "sh",
        str(SRC / "configure"),
        f"--build={build_triplet}",
        f"--host={TRIPLE}",
        "--prefix=/usr",
        "--disable-shared",
        "--enable-static",
        "--disable-threaded-resolver",
        "--disable-pthreads",
        f"--with-openssl={SYSROOT}",
        f"--with-zlib={SYSROOT}",
        "--with-ca-bundle=/etc/ssl/certs/ca-certificates.crt",
        "--without-libpsl",
        "--without-brotli",
        "--without-zstd",
        "--without-nghttp2",
        "--without-libidn2",
        "--without-librtmp",
        "--without-libssh2",
        "--disable-ldap",
        "--disable-ldaps",
        "--disable-manual",
        "--disable-docs",
        "--disable-ntlm",
    ]
    subprocess.run(configure_cmd, cwd=BUILD, env=env, check=True)
    subprocess.run(["make", f"-j{JOBS}"], cwd=BUILD, env=env, check=True)

    out_bin = DESTDIR / "usr/bin/curl"
    out_bin.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(BUILD / "src/curl", out_bin)
    subprocess.run(
        [
            str(CROSS_BIN / f"{TRIPLE}-strip"),
            "--strip-debug",
            "--keep-section=submilli.target",
            str(out_bin),
        ],
        check=True,
    )
    out_bin.chmod(0o755)
    print(f"Built curl-8.12.1 -> {out_bin} ({out_bin.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
