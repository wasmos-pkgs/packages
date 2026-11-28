#!/usr/bin/env python3
"""Cross-compile shared (`liblzma.so.5`) and static (`liblzma.a`) `xz-5.6.4` for
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
JOBS = os.environ.get("WASMOS_JOBS", str(os.cpu_count() or 4))
CROSS_BIN = TOOLCHAIN / "bin"
TRIPLE = "wasm64-submilli-linux-musl"


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    config_sub = SRC / "build-aux/config.sub"
    if config_sub.is_file():
        text = config_sub.read_text(encoding="utf-8", errors="replace")
        if "wasm64" not in text:
            config_sub.write_text(text.replace("wasm32", "wasm32 | wasm64"), encoding="utf-8")

    build_triplet = subprocess.check_output(["sh", str(SRC / "build-aux/config.guess")], text=True).strip()
    strip_bin = str(CROSS_BIN / f"{TRIPLE}-strip")
    env = os.environ.copy()
    env["PATH"] = f"{CROSS_BIN}:{env.get('PATH', '')}"
    env["CC"] = str(CROSS_BIN / f"{TRIPLE}-clang")
    env["AR"] = str(CROSS_BIN / f"{TRIPLE}-ar")
    env["RANLIB"] = str(CROSS_BIN / f"{TRIPLE}-ranlib")
    env["STRIP"] = strip_bin
    env["CFLAGS"] = "-O2 -fPIC"

    subprocess.run(
        [
            "sh",
            str(SRC / "configure"),
            f"--build={build_triplet}",
            f"--host={TRIPLE}",
            "--prefix=/usr",
            "--libdir=/usr/lib",
            "--includedir=/usr/include",
            "--enable-shared",
            "--enable-static",
            "--with-gnu-ld",
            "--enable-threads=posix",
            "--disable-symbol-versions",
            "--disable-xz",
            "--disable-xzdec",
            "--disable-lzmadec",
            "--disable-lzmainfo",
            "--disable-lzma-links",
            "--disable-scripts",
            "--disable-doc",
            "--disable-nls",
            "lt_cv_prog_gnu_ld=yes",
            "lt_cv_deplibs_check_method=pass_all",
        ],
        cwd=BUILD,
        env=env,
        check=True,
    )
    subprocess.run(["make", f"-j{JOBS}"], cwd=BUILD, env=env, check=True)
    subprocess.run(["make", f"DESTDIR={DESTDIR}", "install"], cwd=BUILD, env=env, check=True)

    for la in list(DESTDIR.rglob("*.la")):
        la.unlink()

    so_link = DESTDIR / "usr/lib/liblzma.so.5"
    if not so_link.exists():
        raise SystemExit(f"Expected shared library {so_link} was not built")

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

    so_real = so_link.resolve()
    print(f"Built xz-5.6.4 -> {so_real} ({so_real.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
