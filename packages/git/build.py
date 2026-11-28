#!/usr/bin/env python3
"""Cross-compile dynamic PIE `git-2.56.1` (`/usr/bin/git` + `/usr/libexec/git-core/*`
including HTTPS transport via `libcurl` + `openssl` + `zlib`) for `wasm64-submilli-linux-musl`.
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
    for req_lib in ("libz.so", "libssl.so", "libcurl.so"):
        if not (SYSROOT / "lib" / req_lib).exists():
            raise SystemExit(f"Missing {SYSROOT / 'lib' / req_lib}; install build_deps first")

    # Git builds cleanly in-tree; copy SRC into BUILD to keep SRC clean.
    work_src = BUILD / "git-src"
    if work_src.exists():
        shutil.rmtree(work_src)
    shutil.copytree(SRC, work_src, symlinks=True)

    strip_bin = str(CROSS_BIN / f"{TRIPLE}-strip")
    cc = str(CROSS_BIN / f"{TRIPLE}-clang")
    ar = str(CROSS_BIN / f"{TRIPLE}-ar")

    env = os.environ.copy()
    env["PATH"] = f"{CROSS_BIN}:{env.get('PATH', '')}"

    make_vars = [
        f"CC={cc}",
        f"AR={ar}",
        "uname_S=Linux",
        "uname_M=wasm64",
        "uname_R=6.6.0",
        "HOST_CPU=wasm64",
        "prefix=/usr",
        "gitexecdir=libexec/git-core",
        "SHELL_PATH=/bin/sh",
        f"CFLAGS=-O2 -fPIC -I{SYSROOT / 'include'}",
        f"LDFLAGS=-L{SYSROOT / 'lib'} -Wl,-rpath,/usr/lib",
        "CURL_CONFIG=no",
        f"CURL_LIBCURL=-L{SYSROOT / 'lib'} -lcurl -lssl -lcrypto -lz",
        f"ZLIB_PATH={SYSROOT}",
        "NO_TCLTK=YesPlease",
        "NO_GETTEXT=YesPlease",
        "NO_PERL=YesPlease",
        "NO_PYTHON=YesPlease",
        "NO_EXPAT=YesPlease",
        "NO_SVN_TESTS=YesPlease",
        "NO_REGEX=YesPlease",
        "NO_R_TO_GCC_LINKER=YesPlease",
        "NO_INSTALL_HARDLINKS=YesPlease",
        "INSTALL_SYMLINKS=1",
        "HAVE_PATHS_H=YesPlease",
        "HAVE_GETDELIM=YesPlease",
        "FREAD_READS_DIRECTORIES=UnfortunatelyYes",
        "SNPRINTF_RETURNS_BOGUS=",
        "ICONV_OMITS_BOM=YesPlease",
    ]

    subprocess.run(["make", f"-j{JOBS}", *make_vars, "all"], cwd=work_src, env=env, check=True)
    subprocess.run(
        ["make", f"DESTDIR={DESTDIR}", *make_vars, "install"],
        cwd=work_src,
        env=env,
        check=True,
    )

    # Remove unneeded server/docs helpers if any; keep core git + https transport + templates
    for extra in (
        DESTDIR / "usr/share/locale",
        DESTDIR / "usr/share/man",
        DESTDIR / "usr/share/doc",
        DESTDIR / "usr/share/perl5",
        DESTDIR / "usr/lib",
    ):
        if extra.is_dir():
            shutil.rmtree(extra)

    git_bin = DESTDIR / "usr/bin/git"
    https_helper = DESTDIR / "usr/libexec/git-core/git-remote-https"
    if not git_bin.is_file():
        raise SystemExit("Expected /usr/bin/git was not installed")
    if not (https_helper.is_file() or https_helper.is_symlink()):
        raise SystemExit("Expected /usr/libexec/git-core/git-remote-https was not installed")
    if b"dylink.0" not in git_bin.read_bytes()[:256]:
        raise SystemExit("Expected /usr/bin/git to be dynamically linked (missing dylink.0)")

    wasm_count = 0
    for p in sorted(DESTDIR.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        magic = p.read_bytes()[:4]
        if magic == b"\x00asm":
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
            wasm_count += 1
        elif magic.startswith(b"#!"):
            p.chmod(0o755)
        else:
            p.chmod(0o644)

    print(
        f"Built git-2.56.1 -> {git_bin} ({git_bin.stat().st_size} bytes), "
        f"{wasm_count} Wasm binaries total",
        flush=True,
    )


if __name__ == "__main__":
    main()
