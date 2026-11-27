#!/usr/bin/env python3
"""Cross-compile GNU Bash 5.3 (+ GNU Readline 8.3) for `wasm64-submilli-linux-musl`
-> `$WASMOS_DESTDIR/bin/bash`.
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
    if not (SYSROOT / "lib/libncursesw.a").exists():
        raise SystemExit(f"Missing {SYSROOT / 'lib/libncursesw.a'}; install ncurses build_dep first")

    for helper in ("config.sub", "config.guess", "mkversion.sh", "mksignames"):
        p = SRC / "support" / helper
        if p.is_file():
            p.chmod(0o755)

    BUILD.mkdir(parents=True, exist_ok=True)
    host_cc = shutil.which("cc") or shutil.which("gcc") or "/usr/bin/cc"
    build_triplet = subprocess.check_output(["sh", str(SRC / "support/config.guess")], text=True).strip()

    env = os.environ.copy()
    env["PATH"] = f"{CROSS_BIN}:{env.get('PATH', '')}"
    env["CC"] = str(CROSS_BIN / f"{TRIPLE}-clang")
    env["AR"] = str(CROSS_BIN / f"{TRIPLE}-ar")
    env["RANLIB"] = str(CROSS_BIN / f"{TRIPLE}-ranlib")
    env["STRIP"] = str(CROSS_BIN / f"{TRIPLE}-strip")
    env["CC_FOR_BUILD"] = host_cc
    env["CFLAGS"] = "-O2"
    env["CFLAGS_FOR_BUILD"] = "-O2 -std=gnu17 -Wno-implicit-function-declaration"

    cross_cache_vars = [
        "bash_cv_func_sigsetjmp=present",
        "bash_cv_job_control_missing=present",
        "bash_cv_sys_named_pipes=present",
        "bash_cv_getcwd_malloc=yes",
        "bash_cv_unusable_rtsigs=no",
        "bash_cv_printf_a_format=yes",
        "bash_cv_wcwidth_broken=no",
        "bash_cv_wexitstatus_offset=8",
        "bash_cv_func_ctype_nonascii=yes",
        "bash_cv_func_strtoimax=yes",
        "bash_cv_must_reinstall_sighandlers=no",
        "ac_cv_func_mmap_fixed_mapped=yes",
        "ac_cv_func_strcoll_works=yes",
        "ac_cv_func_working_mktime=yes",
    ]

    configure_cmd = [
        "sh",
        str(SRC / "configure"),
        f"--build={build_triplet}",
        f"--host={TRIPLE}",
        "--prefix=/usr",
        "--bindir=/bin",
        "--without-bash-malloc",
        "--disable-nls",
        "--enable-static-link",
        "--with-curses",
        "--enable-readline",
        "--enable-history",
        "--enable-bang-history",
        "--enable-job-control",
        "--enable-alias",
        "--enable-arith-for-command",
        "--enable-array-variables",
        "--enable-brace-expansion",
        "--enable-casemod-attributes",
        "--enable-casemod-expansions",
        "--enable-command-timing",
        "--enable-cond-command",
        "--enable-cond-regexp",
        "--enable-coprocesses",
        "--enable-directory-stack",
        "--enable-dparen-arithmetic",
        "--enable-extended-glob",
        "--enable-help-builtin",
        "--enable-multibyte",
        "--enable-process-substitution",
        "--enable-progcomp",
        "--enable-prompt-string-decoding",
        *cross_cache_vars,
    ]
    subprocess.run(configure_cmd, cwd=BUILD, env=env, check=True)
    subprocess.run(["make", f"-j{JOBS}"], cwd=BUILD, env=env, check=True)

    out_bin = DESTDIR / "bin/bash"
    out_bin.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(BUILD / "bash", out_bin)
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
    print(f"Built GNU Bash 5.3 -> {out_bin} ({out_bin.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
