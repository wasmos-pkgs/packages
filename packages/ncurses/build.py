#!/usr/bin/env python3
"""Cross-compile shared (`libncursesw.so.6`, `libpanelw.so.6`) and static
`ncurses-6.5` (wide-char + built-in terminfo fallbacks) for
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

FALLBACK_CANDIDATES = [
    "xterm",
    "xterm-256color",
    "vt100",
    "vt102",
    "linux",
    "screen",
    "screen-256color",
    "tmux",
    "tmux-256color",
    "dumb",
]


def available_fallback_terms(infocmp: str) -> str:
    supported: list[str] = []
    for term in FALLBACK_CANDIDATES:
        res = subprocess.run(
            [infocmp, term],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if res.returncode == 0:
            supported.append(term)
    return ",".join(supported)


def symlink_force(target: str, link: pathlib.Path) -> None:
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(target, link)


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    install_prefix = DESTDIR / "usr"
    install_prefix.mkdir(parents=True, exist_ok=True)

    infocmp = shutil.which("infocmp") or "/usr/bin/infocmp"
    host_cc = shutil.which("cc") or shutil.which("gcc") or "/usr/bin/cc"
    true_bin = shutil.which("true") or "/usr/bin/true"
    build_triplet = subprocess.check_output(["sh", str(SRC / "config.guess")], text=True).strip()

    cc = str(CROSS_BIN / f"{TRIPLE}-clang")
    strip_bin = str(CROSS_BIN / f"{TRIPLE}-strip")
    env = os.environ.copy()
    env["PATH"] = f"{CROSS_BIN}:{env.get('PATH', '')}"
    env["CC"] = cc
    env["AR"] = str(CROSS_BIN / f"{TRIPLE}-ar")
    env["RANLIB"] = str(CROSS_BIN / f"{TRIPLE}-ranlib")
    env["STRIP"] = strip_bin
    env["BUILD_CC"] = host_cc
    env["CFLAGS"] = "-O2 -fPIC"

    configure_cmd = [
        "sh",
        str(SRC / "configure"),
        f"--build={build_triplet}",
        f"--host={TRIPLE}",
        "--prefix=/usr",
        "--includedir=/usr/include",
        "--libdir=/usr/lib",
        "--enable-widec",
        "--enable-overwrite",
        "--without-shared",
        "--with-normal",
        "--without-debug",
        "--without-ada",
        "--without-cxx",
        "--without-cxx-binding",
        "--without-progs",
        "--without-tests",
        "--without-manpages",
        "--disable-db-install",
        "--disable-stripping",
        f"--with-tic-path={true_bin}",
        f"--with-infocmp-path={infocmp}",
        f"--with-fallbacks={available_fallback_terms(infocmp)}",
    ]
    subprocess.run(configure_cmd, cwd=BUILD, env=env, check=True)
    subprocess.run(["make", f"-j{JOBS}"], cwd=BUILD, env=env, check=True)
    subprocess.run(
        ["make", f"DESTDIR={DESTDIR}", "install.libs", "install.includes"],
        cwd=BUILD,
        env=env,
        check=True,
    )

    shutil.rmtree(install_prefix / "bin", ignore_errors=True)

    lib_dir = install_prefix / "lib"
    for alias in ("libncurses.a", "libtinfo.a", "libtermcap.a", "libcurses.a"):
        symlink_force("libncursesw.a", lib_dir / alias)
    if (lib_dir / "libpanelw.a").exists():
        symlink_force("libpanelw.a", lib_dir / "libpanel.a")

    so_ncurses = lib_dir / "libncursesw.so.6.5"
    subprocess.run(
        [
            cc,
            "-shared",
            "-Wl,-soname,libncursesw.so.6",
            "-Wl,--whole-archive",
            str(lib_dir / "libncursesw.a"),
            "-Wl,--no-whole-archive",
            "-lc",
            "-o",
            str(so_ncurses),
        ],
        check=True,
    )
    symlink_force("libncursesw.so.6.5", lib_dir / "libncursesw.so.6")
    symlink_force("libncursesw.so.6", lib_dir / "libncursesw.so")
    for alias in ("libncurses.so", "libtinfo.so.6", "libtinfo.so", "libtermcap.so", "libcurses.so"):
        symlink_force("libncursesw.so.6", lib_dir / alias)

    if (lib_dir / "libpanelw.a").exists():
        so_panel = lib_dir / "libpanelw.so.6.5"
        subprocess.run(
            [
                cc,
                "-shared",
                "-Wl,-soname,libpanelw.so.6",
                "-Wl,--whole-archive",
                str(lib_dir / "libpanelw.a"),
                "-Wl,--no-whole-archive",
                f"-L{lib_dir}",
                "-lncursesw",
                "-lc",
                "-o",
                str(so_panel),
            ],
            check=True,
        )
        symlink_force("libpanelw.so.6.5", lib_dir / "libpanelw.so.6")
        symlink_force("libpanelw.so.6", lib_dir / "libpanelw.so")
        symlink_force("libpanelw.so.6", lib_dir / "libpanel.so")

    inc_ncursesw = install_prefix / "include/ncursesw"
    inc_ncurses = install_prefix / "include/ncurses"
    if inc_ncursesw.is_dir() and not inc_ncurses.exists():
        symlink_force("ncursesw", inc_ncurses)

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

    print(f"Built ncurses-6.5 -> {so_ncurses} ({so_ncurses.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
