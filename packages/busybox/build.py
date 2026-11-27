#!/usr/bin/env python3
"""Cross-compile BusyBox for `wasm64-submilli-linux-musl` -> `$WASMOS_DESTDIR/bin/busybox`."""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

SRC = pathlib.Path(os.environ["WASMOS_SRC"])
BUILD = pathlib.Path(os.environ["WASMOS_BUILD"])
DESTDIR = pathlib.Path(os.environ["WASMOS_DESTDIR"])
TOOLCHAIN = pathlib.Path(os.environ["WASMOS_TOOLCHAIN"])
JOBS = os.environ.get("WASMOS_JOBS", str(os.cpu_count() or 4))
CROSS = f"{TOOLCHAIN / 'bin/wasm64-submilli-linux-musl-'}"
HOSTCC = shutil.which("cc") or shutil.which("gcc") or "/usr/bin/cc"

ENABLED_OPTIONS = [
    # General / static musl build
    "CONFIG_STATIC",
    "CONFIG_LFS",
    "CONFIG_LONG_OPTS",
    "CONFIG_SHOW_USAGE",
    "CONFIG_FEATURE_VERBOSE_USAGE",
    "CONFIG_PLATFORM_LINUX",
    "CONFIG_FEATURE_EDITING",
    "CONFIG_FEATURE_TAB_COMPLETION",
    "CONFIG_FEATURE_NON_POSIX_CP",
    "CONFIG_NO_DEBUG_LIB",
    # Shell (`ash` + `sh`)
    "CONFIG_SH_IS_ASH",
    "CONFIG_BASH_IS_NONE",
    "CONFIG_ASH",
    "CONFIG_FEATURE_EDITING_FANCY_PROMPT",
    "CONFIG_FEATURE_LS_COLOR",
    "CONFIG_FEATURE_LS_COLOR_IS_DEFAULT",
    "CONFIG_ASH_OPTIMIZE_FOR_SIZE",
    "CONFIG_ASH_INTERNAL_GLOB",
    "CONFIG_ASH_BASH_COMPAT",
    "CONFIG_ASH_JOB_CONTROL",
    "CONFIG_ASH_ALIAS",
    "CONFIG_ASH_RANDOM_SUPPORT",
    "CONFIG_ASH_EXPAND_PRMT",
    "CONFIG_ASH_ECHO",
    "CONFIG_ASH_PRINTF",
    "CONFIG_ASH_TEST",
    "CONFIG_ASH_HELP",
    "CONFIG_ASH_GETOPTS",
    "CONFIG_ASH_CMDCMD",
    "CONFIG_FEATURE_SH_MATH",
    "CONFIG_FEATURE_SH_MATH_64",
    "CONFIG_FEATURE_SH_STANDALONE",
    "CONFIG_FEATURE_SH_NOFORK",
    # Coreutils
    "CONFIG_BASENAME", "CONFIG_CAT", "CONFIG_CHMOD", "CONFIG_CHOWN",
    "CONFIG_CHROOT", "CONFIG_CKSUM", "CONFIG_COMM", "CONFIG_CP",
    "CONFIG_CUT", "CONFIG_DATE", "CONFIG_DD", "CONFIG_DF",
    "CONFIG_DIRNAME", "CONFIG_DOS2UNIX", "CONFIG_DU", "CONFIG_ECHO",
    "CONFIG_ENV", "CONFIG_EXPAND", "CONFIG_EXPR", "CONFIG_EXPR_MATH_SUPPORT_64",
    "CONFIG_FACTOR", "CONFIG_FALSE", "CONFIG_FOLD", "CONFIG_HEAD",
    "CONFIG_FEATURE_FANCY_HEAD", "CONFIG_ID", "CONFIG_INSTALL", "CONFIG_JOIN",
    "CONFIG_LINK", "CONFIG_LN", "CONFIG_LS", "CONFIG_FEATURE_LS_FILETYPES",
    "CONFIG_FEATURE_LS_FOLLOWLINKS", "CONFIG_FEATURE_LS_RECURSIVE",
    "CONFIG_FEATURE_LS_SORTFILES", "CONFIG_FEATURE_LS_TIMESTAMPS",
    "CONFIG_FEATURE_LS_USERNAME", "CONFIG_MD5SUM", "CONFIG_SHA1SUM",
    "CONFIG_SHA256SUM", "CONFIG_SHA512SUM", "CONFIG_MKDIR", "CONFIG_MKFIFO",
    "CONFIG_MKNOD", "CONFIG_MKTEMP", "CONFIG_MV", "CONFIG_NL",
    "CONFIG_NOHUP", "CONFIG_NPROC", "CONFIG_OD", "CONFIG_PASTE",
    "CONFIG_PRINTENV", "CONFIG_PRINTF", "CONFIG_PWD", "CONFIG_READLINK",
    "CONFIG_FEATURE_READLINK_FOLLOW", "CONFIG_REALPATH", "CONFIG_RM",
    "CONFIG_RMDIR", "CONFIG_SEQ", "CONFIG_SHRED", "CONFIG_SHUF",
    "CONFIG_SLEEP", "CONFIG_FEATURE_FANCY_SLEEP", "CONFIG_SORT",
    "CONFIG_SPLIT", "CONFIG_STAT", "CONFIG_STTY", "CONFIG_SYNC",
    "CONFIG_TAC", "CONFIG_TAIL", "CONFIG_FEATURE_FANCY_TAIL", "CONFIG_TEE",
    "CONFIG_TEST", "CONFIG_TEST1", "CONFIG_TEST2", "CONFIG_FEATURE_TEST_64",
    "CONFIG_TIMEOUT", "CONFIG_TOUCH", "CONFIG_TR", "CONFIG_TRUE",
    "CONFIG_TRUNCATE", "CONFIG_TTY", "CONFIG_UNAME", "CONFIG_UNEXPAND",
    "CONFIG_UNIQ", "CONFIG_UNLINK", "CONFIG_USLEEP", "CONFIG_UUDECODE",
    "CONFIG_UUENCODE", "CONFIG_BASE64", "CONFIG_WC", "CONFIG_WHOAMI",
    "CONFIG_YES",
    # Findutils / Editors / Text processing
    "CONFIG_FIND", "CONFIG_FEATURE_FIND_PRINT0", "CONFIG_FEATURE_FIND_MTIME",
    "CONFIG_FEATURE_FIND_PERM", "CONFIG_FEATURE_FIND_TYPE",
    "CONFIG_FEATURE_FIND_MAXDEPTH", "CONFIG_FEATURE_FIND_NEWER",
    "CONFIG_FEATURE_FIND_INUM", "CONFIG_FEATURE_FIND_EXEC",
    "CONFIG_FEATURE_FIND_NAME", "CONFIG_FEATURE_FIND_PATH",
    "CONFIG_FEATURE_FIND_REGEX", "CONFIG_FEATURE_FIND_DELETE",
    "CONFIG_GREP", "CONFIG_EGREP", "CONFIG_FGREP", "CONFIG_FEATURE_GREP_CONTEXT",
    "CONFIG_XARGS", "CONFIG_AWK", "CONFIG_FEATURE_AWK_LIBM",
    "CONFIG_CMP", "CONFIG_DIFF", "CONFIG_ED", "CONFIG_PATCH",
    "CONFIG_SED", "CONFIG_VI",
    # Archival
    "CONFIG_TAR", "CONFIG_FEATURE_TAR_CREATE", "CONFIG_FEATURE_TAR_LONG_OPTIONS",
    "CONFIG_GZIP", "CONFIG_GUNZIP", "CONFIG_ZCAT",
    "CONFIG_BZIP2", "CONFIG_BUNZIP2", "CONFIG_BZCAT",
    "CONFIG_UNXZ", "CONFIG_XZ", "CONFIG_CPIO", "CONFIG_UNZIP",
    # Process & Misc utilities
    "CONFIG_FREE", "CONFIG_KILL", "CONFIG_KILLALL", "CONFIG_PGREP",
    "CONFIG_PKILL", "CONFIG_PIDOF", "CONFIG_PS", "CONFIG_PSTREE",
    "CONFIG_PWDX", "CONFIG_UPTIME", "CONFIG_WATCH",
    "CONFIG_WHICH", "CONFIG_LESS", "CONFIG_HEXDUMP", "CONFIG_XXD",
    "CONFIG_STRINGS", "CONFIG_TIME", "CONFIG_CLEAR", "CONFIG_RESET",
    # Networking utilities
    "CONFIG_FEATURE_IPV6",
    "CONFIG_NC", "CONFIG_NC_SERVER", "CONFIG_NC_EXTRA",
    "CONFIG_WGET", "CONFIG_FEATURE_WGET_LONG_OPTIONS", "CONFIG_FEATURE_WGET_STATUSBAR",
    "CONFIG_FEATURE_WGET_HTTPS",
    "CONFIG_NSLOOKUP", "CONFIG_WHOIS", "CONFIG_TELNET",
    "CONFIG_HTTPD", "CONFIG_IFCONFIG", "CONFIG_FEATURE_IFCONFIG_STATUS", "CONFIG_NETSTAT",
    "CONFIG_IP", "CONFIG_FEATURE_IP_ADDRESS", "CONFIG_FEATURE_IP_LINK", "CONFIG_FEATURE_IP_ROUTE",
]


def run_make(*args: str, stdin_text: str | None = None) -> None:
    cmd = [
        "make",
        "-C", str(SRC),
        f"O={BUILD}",
        f"HOSTCC={HOSTCC}",
        f"CROSS_COMPILE={CROSS}",
        *args,
    ]
    subprocess.run(cmd, input=stdin_text, text=True, check=True)


def set_config_options(cfg_path: pathlib.Path, enabled: list[str]) -> None:
    text = cfg_path.read_text(encoding="utf-8")
    for opt in enabled:
        unset_pat = re.compile(rf"^#\s*{re.escape(opt)}\s+is not set\s*$", re.MULTILINE)
        set_pat = re.compile(rf"^{re.escape(opt)}=.*$", re.MULTILINE)
        if unset_pat.search(text):
            text = unset_pat.sub(f"{opt}=y", text)
        elif set_pat.search(text):
            text = set_pat.sub(f"{opt}=y", text)
        else:
            text += f"\n{opt}=y\n"
    text = re.sub(r"^CONFIG_SH_IS_NONE=y\s*$", "# CONFIG_SH_IS_NONE is not set", text, flags=re.MULTILINE)
    cfg_path.write_text(text, encoding="utf-8")


def main() -> None:
    sysroot_env = os.environ.get("WASMOS_SYSROOT")
    if sysroot_env:
        compiler_h = pathlib.Path(sysroot_env) / "include/linux/compiler.h"
        if not compiler_h.exists():
            compiler_h.parent.mkdir(parents=True, exist_ok=True)
            compiler_h.write_text(
                "#ifndef _LINUX_COMPILER_H\n#define _LINUX_COMPILER_H\n"
                "#define __user\n#define __force\n#define __iomem\n#define __rcu\n#endif\n",
                encoding="utf-8",
            )
    BUILD.mkdir(parents=True, exist_ok=True)
    run_make("allnoconfig")

    cfg = BUILD / ".config"
    set_config_options(cfg, ENABLED_OPTIONS)
    run_make("oldconfig", stdin_text="\n" * 512)
    run_make(f"-j{JOBS}")

    out_bin = DESTDIR / "bin/busybox"
    out_bin.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(BUILD / "busybox_unstripped", out_bin)
    subprocess.run(
        [
            f"{CROSS}strip",
            "--strip-debug",
            "--keep-section=submilli.target",
            str(out_bin),
        ],
        check=True,
    )
    out_bin.chmod(0o755)
    print(f"Built busybox -> {out_bin} ({out_bin.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
