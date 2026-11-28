#!/usr/bin/env python3
"""Repackage the pinned `llvm-*-linux-wasm64.tar.zst` release from
`wasmos-pkgs/toolchain` into `$WASMOS_DESTDIR/usr/{bin,lib/clang}` for
native self-hosted C/C++20 compilation inside WasmOS.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import toml_loader as tomllib  # noqa: E402

BUILD = pathlib.Path(os.environ["WASMOS_BUILD"])
DESTDIR = pathlib.Path(os.environ["WASMOS_DESTDIR"])
SYSROOT = pathlib.Path(os.environ["WASMOS_SYSROOT"])
TRIPLE = "wasm64-submilli-linux-musl"


def load_fetch_module():
    spec = importlib.util.spec_from_file_location(
        "fetch_toolchain", REPO_ROOT / "scripts/fetch-toolchain.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def extract_local_zst(archive: pathlib.Path, dest: pathlib.Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    zstd_proc = subprocess.Popen(["zstd", "-dc", str(archive)], stdout=subprocess.PIPE)
    assert zstd_proc.stdout is not None
    tar_proc = subprocess.Popen(["tar", "-xf", "-", "-C", str(dest)], stdin=zstd_proc.stdout)
    zstd_proc.stdout.close()
    if tar_proc.wait() != 0 or zstd_proc.wait() != 0:
        raise SystemExit(f"Failed to extract {archive}")


def symlink_force(target: str, link: pathlib.Path) -> None:
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(target, link)


def write_exec(path: pathlib.Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def main() -> None:
    cfg = tomllib.loads((REPO_ROOT / "toolchain.toml").read_text(encoding="utf-8"))["toolchain"]
    repo = str(cfg["repo"])
    tag = str(cfg["llvm"])
    asset = f"{tag}-linux-wasm64.tar.zst"

    unpacked = BUILD / "unpacked"
    if unpacked.exists():
        shutil.rmtree(unpacked)

    local_dir = os.environ.get("WASMOS_LOCAL_TC_DIST", "").strip()
    if local_dir and (pathlib.Path(local_dir) / asset).is_file():
        extract_local_zst(pathlib.Path(local_dir) / asset, unpacked)
    else:
        fetch_tc = load_fetch_module()
        cache_dir = REPO_ROOT / "build/_toolchain-cache"
        fetch_tc.fetch_and_extract(repo, tag, asset, cache_dir, unpacked)

    src_bin = unpacked / "llvm/bin"
    src_clang_lib = unpacked / "llvm/lib/clang"
    if not (src_bin / "clang-21").is_file() or not (src_bin / "lld").is_file():
        raise SystemExit(f"Incomplete llvm linux-wasm64 archive at {unpacked}")

    usr_bin = DESTDIR / "usr/bin"
    usr_lib_clang = DESTDIR / "usr/lib/clang"
    usr_bin.mkdir(parents=True, exist_ok=True)
    usr_lib_clang.mkdir(parents=True, exist_ok=True)

    # 1. Install wasm64 LLVM binaries into /usr/bin
    for tool in ("clang-21", "lld", "llvm-ar", "llvm-nm", "llvm-objcopy", "llvm-objdump"):
        src = src_bin / tool
        if not src.is_file():
            raise SystemExit(f"Missing {tool} in {src_bin}")
        if src.read_bytes()[:4] != b"\x00asm":
            raise SystemExit(f"{tool} is not a Wasm binary: {src}")
        dst = usr_bin / tool
        shutil.copy2(src, dst)
        dst.chmod(0o755)

    # 2. Standard POSIX & LLVM symlinks in /usr/bin
    for link_name, target_name in [
        ("clang", "clang-21"),
        ("clang++", "clang-21"),
        ("cc", "clang-21"),
        ("c++", "clang-21"),
        ("gcc", "clang-21"),
        ("g++", "clang-21"),
        ("wasm-ld", "lld"),
        ("ld", "lld"),
        ("llvm-ranlib", "llvm-ar"),
        ("ar", "llvm-ar"),
        ("ranlib", "llvm-ar"),
        ("nm", "llvm-nm"),
        ("llvm-strip", "llvm-objcopy"),
        ("strip", "llvm-objcopy"),
        ("objcopy", "llvm-objcopy"),
        ("objdump", "llvm-objdump"),
        (f"{TRIPLE}-clang", "clang-21"),
        (f"{TRIPLE}-clang++", "clang-21"),
        (f"{TRIPLE}-cc", "clang-21"),
        (f"{TRIPLE}-c++", "clang-21"),
        (f"{TRIPLE}-gcc", "clang-21"),
        (f"{TRIPLE}-g++", "clang-21"),
        (f"{TRIPLE}-ar", "llvm-ar"),
        (f"{TRIPLE}-ranlib", "llvm-ar"),
        (f"{TRIPLE}-nm", "llvm-nm"),
        (f"{TRIPLE}-strip", "llvm-objcopy"),
        (f"{TRIPLE}-objcopy", "llvm-objcopy"),
        (f"{TRIPLE}-objdump", "llvm-objdump"),
        (f"{TRIPLE}-ld", "lld"),
    ]:
        symlink_force(target_name, usr_bin / link_name)

    # 3. Default Clang config so `clang`/`clang++`/`cc`/`c++` use /usr as sysroot automatically
    cfg_content = "--sysroot=/usr\n"
    for cfg_name in (f"{TRIPLE}.cfg", "clang.cfg", "clang++.cfg"):
        cfg_file = usr_bin / cfg_name
        cfg_file.write_text(cfg_content, encoding="utf-8")
        cfg_file.chmod(0o644)

    # 4. Preprocessor & Rust linker wrappers
    write_exec(usr_bin / "cpp", '#!/bin/sh\nexec /usr/bin/clang -E "$@"\n')
    write_exec(usr_bin / f"{TRIPLE}-cpp", '#!/bin/sh\nexec /usr/bin/clang -E "$@"\n')
    write_exec(
        usr_bin / f"{TRIPLE}-rust-linker",
        f'#!/bin/sh\nexec /usr/bin/clang -static "$@" --target={TRIPLE} --sysroot=/usr /usr/lib/libclang_rt.builtins-wasm64.a\n',
    )

    # 5. Clang resource headers + compiler-rt builtins in /usr/lib/clang/<major>/lib/...
    if src_clang_lib.is_dir():
        shutil.copytree(src_clang_lib, usr_lib_clang, symlinks=True, dirs_exist_ok=True)
    builtins_a = SYSROOT / "lib/libclang_rt.builtins-wasm64.a"
    if not builtins_a.is_file():
        raise SystemExit(f"Missing {builtins_a} in toolchain sysroot")
    res_versions = sorted(p for p in usr_lib_clang.iterdir() if p.is_dir())
    if not res_versions:
        raise SystemExit(f"No Clang resource directory found under {usr_lib_clang}")
    res_dir = res_versions[-1]
    for dst in (
        res_dir / "lib/linux/libclang_rt.builtins-wasm64.a",
        res_dir / "lib/wasm64-unknown-linux-musl/libclang_rt.builtins.a",
    ):
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(builtins_a, dst)
        dst.chmod(0o644)

    print(f"Packaged clang ({tag}) -> {usr_bin} and {res_dir}", flush=True)


if __name__ == "__main__":
    main()
