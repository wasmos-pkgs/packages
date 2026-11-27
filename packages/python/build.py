#!/usr/bin/env python3
"""Cross-compile batteries-included static `CPython 3.13.2` for
`wasm64-submilli-linux-musl` -> `$WASMOS_DESTDIR/usr/{bin,lib/python3.13}`.
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


def patch_cpython_source(py_src: pathlib.Path) -> None:
    config_sub = py_src / "config.sub"
    if config_sub.is_file():
        text = config_sub.read_text(encoding="utf-8", errors="replace")
        if "wasm64" not in text:
            config_sub.write_text(text.replace("wasm32", "wasm32 | wasm64"), encoding="utf-8")

    triplet_c = py_src / "Misc/platform_triplet.c"
    text = triplet_c.read_text(encoding="utf-8")
    if "PLATFORM_TRIPLET=wasm64-linux-LIBC" not in text:
        target = "# elif defined(__riscv)\n"
        addition = (
            "# elif defined(__wasm64__)\n"
            "PLATFORM_TRIPLET=wasm64-linux-LIBC\n"
            "# elif defined(__riscv)\n"
        )
        if target not in text:
            raise SystemExit("Could not locate __riscv anchor in Misc/platform_triplet.c")
        triplet_c.write_text(text.replace(target, addition, 1), encoding="utf-8")


def build_host_python(py_src: pathlib.Path, host_bdir: pathlib.Path) -> pathlib.Path:
    host_bdir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for k in (
        "CC", "CXX", "CPP", "AR", "RANLIB", "NM", "STRIP",
        "CFLAGS", "CPPFLAGS", "LDFLAGS", "LIBS", "CONFIG_SITE",
        "PKG_CONFIG_LIBDIR", "PKG_CONFIG_PATH",
    ):
        env.pop(k, None)
    env["CC"] = shutil.which("cc") or shutil.which("gcc") or "/usr/bin/cc"
    env["CFLAGS"] = "-O2"

    subprocess.run(
        [
            "sh",
            str(py_src / "configure"),
            "--without-ensurepip",
            "--disable-test-modules",
            "py_cv_module__ssl=n/a",
            "py_cv_module__hashlib=n/a",
            "py_cv_module__tkinter=n/a",
            "py_cv_module__curses=n/a",
            "py_cv_module__curses_panel=n/a",
            "py_cv_module__ctypes=n/a",
            "py_cv_module_readline=n/a",
            "py_cv_module__dbm=n/a",
            "py_cv_module__gdbm=n/a",
            "py_cv_module__sqlite3=n/a",
        ],
        cwd=host_bdir,
        env=env,
        check=True,
    )
    subprocess.run(["make", f"-j{JOBS}"], cwd=host_bdir, env=env, check=True)
    for cand in (host_bdir / "python.exe", host_bdir / "python"):
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    raise SystemExit("Host build-python binary not found")


def main() -> None:
    patch_cpython_source(SRC)
    host_python = build_host_python(SRC, BUILD / "host")

    wasm_bdir = BUILD / "wasm"
    wasm_bdir.mkdir(parents=True, exist_ok=True)

    config_site = wasm_bdir / "config.site"
    config_site.write_text(
        "\n".join(
            [
                "ac_cv_buggy_getaddrinfo=no",
                "ac_cv_file__dev_ptmx=yes",
                "ac_cv_file__dev_ptc=no",
                "ac_cv_func_dlopen=no",
                "ac_cv_lib_dl_dlopen=no",
                "ac_cv_func_posix_spawn=no",
                "ac_cv_func_posix_spawnp=no",
                "ac_cv_func_memfd_create=no",
                "ac_cv_func_preadv=no",
                "ac_cv_func_preadv2=no",
                "ac_cv_func_pwritev=no",
                "ac_cv_func_pwritev2=no",
                "ac_cv_func_pthread_kill=no",
                "ac_cv_header_linux_vm_sockets_h=no",
                "ac_cv_header_linux_can_h=no",
                "ac_cv_header_linux_can_raw_h=no",
                "ac_cv_header_linux_can_bcm_h=no",
                "ac_cv_header_linux_can_j1939_h=no",
                "ac_cv_can_raw_fd_frames=no",
                "ac_cv_can_raw_join_filters=no",
                "ac_cv_header_linux_netlink_h=no",
                "ac_cv_header_linux_qrtr_h=no",
                "ac_cv_header_linux_tipc_h=no",
                "ac_cv_header_linux_if_alg_h=no",
                "ac_cv_header_netpacket_packet_h=no",
                "py_cv_module__ctypes=n/a",
                "py_cv_module__ctypes_test=n/a",
                "py_cv_module__tkinter=n/a",
                "py_cv_module__dbm=n/a",
                "py_cv_module__gdbm=n/a",
                "py_cv_module__uuid=n/a",
                "py_cv_module__posixshmem=n/a",
                "py_cv_module__multiprocessing=n/a",
                "py_cv_module_readline=n/a",
                "py_cv_module_syslog=n/a",
                "py_cv_module_grp=yes",
                "",
            ]
        ),
        encoding="utf-8",
    )

    build_triplet = subprocess.check_output(["sh", str(SRC / "config.guess")], text=True).strip()
    env = os.environ.copy()
    env["PATH"] = f"{CROSS_BIN}:{env.get('PATH', '')}"
    env["CC"] = str(CROSS_BIN / f"{TRIPLE}-clang")
    env["CXX"] = str(CROSS_BIN / f"{TRIPLE}-clang++")
    env["CPP"] = str(CROSS_BIN / f"{TRIPLE}-cpp")
    env["AR"] = str(CROSS_BIN / f"{TRIPLE}-ar")
    env["RANLIB"] = str(CROSS_BIN / f"{TRIPLE}-ranlib")
    env["NM"] = str(CROSS_BIN / f"{TRIPLE}-nm")
    env["STRIP"] = str(CROSS_BIN / f"{TRIPLE}-strip")
    env["READELF"] = shutil.which("true") or "/usr/bin/true"
    env["PKG_CONFIG_LIBDIR"] = str(SYSROOT / "lib/pkgconfig")
    env["PKG_CONFIG_PATH"] = str(SYSROOT / "lib/pkgconfig")
    env["CONFIG_SITE"] = str(config_site)
    env["CFLAGS"] = f"-O2 -I{SYSROOT / 'include'} -I{SYSROOT / 'include/ncursesw'}"
    env["CPPFLAGS"] = f"-I{SYSROOT / 'include'} -I{SYSROOT / 'include/ncursesw'}"
    env["LDFLAGS"] = f"-static -Wl,-z,stack-size=8388608 -L{SYSROOT / 'lib'}"
    env["ZLIB_CFLAGS"] = f"-I{SYSROOT / 'include'}"
    env["ZLIB_LIBS"] = "-lz"
    env["BZIP2_CFLAGS"] = f"-I{SYSROOT / 'include'}"
    env["BZIP2_LIBS"] = "-lbz2"
    env["LIBLZMA_CFLAGS"] = f"-I{SYSROOT / 'include'}"
    env["LIBLZMA_LIBS"] = "-llzma"
    env["LIBSQLITE3_CFLAGS"] = f"-I{SYSROOT / 'include'}"
    env["LIBSQLITE3_LIBS"] = "-lsqlite3"
    env["CURSES_CFLAGS"] = f"-I{SYSROOT / 'include'} -I{SYSROOT / 'include/ncursesw'}"
    env["CURSES_LIBS"] = "-lncursesw"
    env["PANEL_CFLAGS"] = f"-I{SYSROOT / 'include'} -I{SYSROOT / 'include/ncursesw'}"
    env["PANEL_LIBS"] = "-lpanelw -lncursesw"
    env["CCSHARED"] = " "
    env["LINKFORSHARED"] = " "
    env["LDSHARED"] = env["CC"]
    env["BLDSHARED"] = env["CC"]
    env["MODULE_BUILDTYPE"] = "static"

    subprocess.run(
        [
            "sh",
            str(SRC / "configure"),
            f"--build={build_triplet}",
            f"--host={TRIPLE}",
            f"--with-build-python={host_python}",
            "--prefix=/usr",
            "--disable-shared",
            "--enable-ipv6",
            "--without-mimalloc",
            "--without-ensurepip",
            "--disable-test-modules",
            f"--with-openssl={SYSROOT}",
            "--with-system-expat=no",
            "--with-system-libmpdec=no",
        ],
        cwd=wasm_bdir,
        env=env,
        check=True,
    )
    subprocess.run(["make", f"-j{JOBS}"], cwd=wasm_bdir, env=env, check=True)
    subprocess.run(["make", f"DESTDIR={DESTDIR}", "install"], cwd=wasm_bdir, env=env, check=True)

    py_bin = DESTDIR / "usr/bin/python3.13"
    subprocess.run(
        [
            str(CROSS_BIN / f"{TRIPLE}-strip"),
            "--strip-debug",
            "--keep-section=submilli.target",
            str(py_bin),
        ],
        check=True,
    )

    # Remove non-Wasm helper scripts in /usr/bin (`python3.13-config`, `idle3*`, `pydoc3*`, `2to3*`)
    for p in list((DESTDIR / "usr/bin").iterdir()):
        if p.name not in ("python3.13", "python3", "python"):
            p.unlink()
    py_sym = DESTDIR / "usr/bin/python"
    if py_sym.exists() or py_sym.is_symlink():
        py_sym.unlink()
    os.symlink("python3", py_sym)

    # Prune static archive, headers, pkgconfig, manpages, and heavy test/GUI stdlib dirs for runtime package
    py_lib = DESTDIR / "usr/lib/python3.13"
    for heavy_dir in ("test", "tests", "idlelib", "tkinter", "turtledemo", "ensurepip", "pydoc_data"):
        p = py_lib / heavy_dir
        if p.exists():
            shutil.rmtree(p)
    for cfg_dir in py_lib.glob("config-3.13*"):
        shutil.rmtree(cfg_dir)
    for extra in (
        DESTDIR / "usr/lib/libpython3.13.a",
        DESTDIR / "usr/lib/pkgconfig",
        DESTDIR / "usr/include",
        DESTDIR / "usr/share/man",
    ):
        if extra.is_dir():
            shutil.rmtree(extra)
        elif extra.exists():
            extra.unlink()
    for pycache in list(py_lib.rglob("__pycache__")):
        shutil.rmtree(pycache, ignore_errors=True)

    # Ensure only the Wasm interpreter binary has +x permission (`verify_outputs` enforces executable == \0asm)
    for p in DESTDIR.rglob("*"):
        if p.is_file() and not p.is_symlink():
            if p == py_bin:
                p.chmod(0o755)
            else:
                p.chmod(0o644)

    print(f"Built CPython 3.13.2 -> {py_bin} ({py_bin.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
