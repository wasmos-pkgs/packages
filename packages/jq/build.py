#!/usr/bin/env python3
"""Cross-compile dynamic PIE `jq-1.8.2` (`/usr/bin/jq` with bundled Oniguruma)
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


def patch_config_sub(root: pathlib.Path) -> None:
    for cfg in root.rglob("config.sub"):
        text = cfg.read_text(encoding="utf-8", errors="replace")
        if "wasm64" not in text:
            if "wasm32" in text:
                cfg.write_text(text.replace("wasm32", "wasm32 | wasm64"), encoding="utf-8")
            else:
                cfg.write_text('#!/bin/sh\necho "$1"\n', encoding="utf-8")
                cfg.chmod(0o755)


def main() -> None:
    patch_config_sub(SRC)
    BUILD.mkdir(parents=True, exist_ok=True)

    config_guess = next(SRC.rglob("config.guess"))
    build_triplet = subprocess.check_output(["sh", str(config_guess)], text=True).strip()
    strip_bin = str(CROSS_BIN / f"{TRIPLE}-strip")

    env = os.environ.copy()
    env["PATH"] = f"{CROSS_BIN}:{env.get('PATH', '')}"
    env["CC"] = str(CROSS_BIN / f"{TRIPLE}-clang")
    env["AR"] = str(CROSS_BIN / f"{TRIPLE}-ar")
    env["RANLIB"] = str(CROSS_BIN / f"{TRIPLE}-ranlib")
    env["STRIP"] = strip_bin
    env["CFLAGS"] = "-O2 -fPIC"
    env["LDFLAGS"] = f"-L{SYSROOT / 'lib'} -Wl,-rpath,/usr/lib"

    configure_cmd = [
        "sh",
        str(SRC / "configure"),
        f"--build={build_triplet}",
        f"--host={TRIPLE}",
        "--prefix=/usr",
        "--with-oniguruma=builtin",
        "--disable-shared",
        "--enable-static",
        "--disable-docs",
        "--disable-valgrind",
        "--disable-maintainer-mode",
        "lt_cv_prog_gnu_ld=yes",
        "lt_cv_deplibs_check_method=pass_all",
    ]
    subprocess.run(configure_cmd, cwd=BUILD, env=env, check=True)
    subprocess.run(["make", f"-j{JOBS}"], cwd=BUILD, env=env, check=True)
    subprocess.run(["make", f"DESTDIR={DESTDIR}", "install"], cwd=BUILD, env=env, check=True)

    # Keep runtime package lean: remove docs, headers, static archives, libtool `.la`
    for prune in (
        DESTDIR / "usr/include",
        DESTDIR / "usr/lib",
        DESTDIR / "usr/share/doc",
        DESTDIR / "usr/share/man",
    ):
        if prune.is_dir():
            shutil.rmtree(prune)

    out_bin = DESTDIR / "usr/bin/jq"
    if not out_bin.is_file():
        raise SystemExit("Expected /usr/bin/jq was not installed")
    if b"dylink.0" not in out_bin.read_bytes()[:256]:
        raise SystemExit("Expected /usr/bin/jq to be dynamically linked (missing dylink.0)")

    subprocess.run(
        [
            strip_bin,
            "--strip-debug",
            "--keep-section=dylink.0",
            "--keep-section=submilli.target",
            "--keep-section=submilli.fork_exports",
            "--keep-section=name",
            str(out_bin),
        ],
        check=True,
    )
    out_bin.chmod(0o755)
    print(f"Built jq-1.8.2 -> {out_bin} ({out_bin.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
