#!/usr/bin/env python3
"""Repackage the pinned `rustc-*-linux-wasm64.tar.zst` release from
`wasmos-pkgs/toolchain` into `$WASMOS_DESTDIR/usr/{bin,lib/rustlib,share/wasmos}`
for native self-hosted Rust compilation inside WasmOS.
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


def main() -> None:
    cfg = tomllib.loads((REPO_ROOT / "toolchain.toml").read_text(encoding="utf-8"))["toolchain"]
    repo = str(cfg["repo"])
    tag = str(cfg["rustc"])
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

    src_rustc = unpacked / "bin/rustc"
    src_rustlib = unpacked / "lib/rustlib" / TRIPLE / "lib"
    if not src_rustc.is_file() or src_rustc.read_bytes()[:4] != b"\x00asm":
        raise SystemExit(f"Invalid wasm64 rustc binary at {src_rustc}")
    if not src_rustlib.is_dir():
        raise SystemExit(f"Missing target stdlib at {src_rustlib}")

    dst_rustc = DESTDIR / "usr/bin/rustc"
    dst_rustc.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_rustc, dst_rustc)
    dst_rustc.chmod(0o755)

    dst_rustlib = DESTDIR / "usr/lib/rustlib" / TRIPLE / "lib"
    shutil.copytree(src_rustlib, dst_rustlib, symlinks=True, dirs_exist_ok=True)

    src_libc = unpacked / "share/wasmos/libc"
    if src_libc.is_dir():
        shutil.copytree(src_libc, DESTDIR / "usr/share/wasmos/libc", symlinks=True, dirs_exist_ok=True)

    print(f"Packaged rustc ({tag}) -> {dst_rustc} ({dst_rustc.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
