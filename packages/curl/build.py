#!/usr/bin/env python3
"""Cross-compile dynamic `curl-8.12.1` (`/usr/bin/curl` + `/usr/lib/libcurl.so.4`)
for `wasm64-submilli-linux-musl`.
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
    if not (SYSROOT / "lib/libssl.so").exists():
        raise SystemExit(f"Missing {SYSROOT / 'lib/libssl.so'}; install openssl build_dep first")

    config_sub = SRC / "config.sub"
    if config_sub.is_file():
        text = config_sub.read_text(encoding="utf-8", errors="replace")
        if "wasm64" not in text:
            config_sub.write_text(text.replace("wasm32", "wasm32 | wasm64"), encoding="utf-8")

    BUILD.mkdir(parents=True, exist_ok=True)
    build_triplet = subprocess.check_output(["sh", str(SRC / "config.guess")], text=True).strip()
    strip_bin = str(CROSS_BIN / f"{TRIPLE}-strip")

    env = os.environ.copy()
    env["PATH"] = f"{CROSS_BIN}:{env.get('PATH', '')}"
    env["CC"] = str(CROSS_BIN / f"{TRIPLE}-clang")
    env["AR"] = str(CROSS_BIN / f"{TRIPLE}-ar")
    env["RANLIB"] = str(CROSS_BIN / f"{TRIPLE}-ranlib")
    env["STRIP"] = strip_bin
    env["PKG_CONFIG_LIBDIR"] = str(SYSROOT / "lib/pkgconfig")
    env["PKG_CONFIG_PATH"] = str(SYSROOT / "lib/pkgconfig")
    env["CFLAGS"] = "-O2 -fPIC"
    env["CPPFLAGS"] = f"-I{SYSROOT / 'include'}"
    env["LDFLAGS"] = f"-L{SYSROOT / 'lib'} -Wl,-rpath,/usr/lib"
    env["LIBS"] = "-lssl -lcrypto -lz"

    configure_cmd = [
        "sh",
        str(SRC / "configure"),
        f"--build={build_triplet}",
        f"--host={TRIPLE}",
        "--prefix=/usr",
        "--libdir=/usr/lib",
        "--enable-shared",
        "--enable-static",
        "--with-gnu-ld",
        "--enable-threaded-resolver",
        "--enable-pthreads",
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
        "lt_cv_prog_gnu_ld=yes",
        "lt_cv_deplibs_check_method=pass_all",
    ]
    subprocess.run(configure_cmd, cwd=BUILD, env=env, check=True)
    subprocess.run(["make", f"-j{JOBS}"], cwd=BUILD, env=env, check=True)
    subprocess.run(["make", f"DESTDIR={DESTDIR}", "install"], cwd=BUILD, env=env, check=True)

    # Prune host shell helper `curl-config`, libtool `.la`, and docs/manpages
    for extra in (
        DESTDIR / "usr/bin/curl-config",
        DESTDIR / "usr/share/man",
        DESTDIR / "usr/share/aclocal",
    ):
        if extra.is_dir():
            shutil.rmtree(extra)
        elif extra.exists() or extra.is_symlink():
            extra.unlink()
    for la in list(DESTDIR.rglob("*.la")):
        la.unlink()

    out_bin = DESTDIR / "usr/bin/curl"
    so_link = DESTDIR / "usr/lib/libcurl.so.4"
    if not out_bin.is_file() or not so_link.exists():
        raise SystemExit("Expected /usr/bin/curl and /usr/lib/libcurl.so.4 were not installed")
    if b"dylink.0" not in out_bin.read_bytes()[:256]:
        raise SystemExit("Expected /usr/bin/curl to be dynamically linked (missing dylink.0)")

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
        f"Built curl-8.12.1 -> {out_bin} ({out_bin.stat().st_size} bytes), "
        f"{so_link.resolve()} ({so_link.resolve().stat().st_size} bytes)",
        flush=True,
    )


if __name__ == "__main__":
    main()
