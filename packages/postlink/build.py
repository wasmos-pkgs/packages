#!/usr/bin/env python3
"""Repackage the pinned `postlink-*-linux-wasm64.tar.zst` release from
`wasmos-pkgs/toolchain` into `$WASMOS_DESTDIR/usr/bin/wasmos-postlink`.
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
    tag = str(cfg["postlink"])
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

    src_bin = unpacked / "bin/wasmos-postlink"
    if not src_bin.is_file() or src_bin.read_bytes()[:4] != b"\x00asm":
        raise SystemExit(f"Invalid wasm64 wasmos-postlink binary at {src_bin}")

    dst_bin = DESTDIR / "usr/bin/wasmos-postlink"
    dst_bin.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_bin, dst_bin)
    dst_bin.chmod(0o755)
    print(f"Packaged postlink ({tag}) -> {dst_bin} ({dst_bin.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
