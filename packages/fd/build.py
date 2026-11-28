#!/usr/bin/env python3
"""Cross-compile `fd` 10.5.2 for `wasm64-submilli-linux-musl`
-> `$WASMOS_DESTDIR/usr/bin/fd`.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import urllib.request

SRC = pathlib.Path(os.environ["WASMOS_SRC"])
BUILD = pathlib.Path(os.environ["WASMOS_BUILD"])
DESTDIR = pathlib.Path(os.environ["WASMOS_DESTDIR"])
TOOLCHAIN = pathlib.Path(os.environ["WASMOS_TOOLCHAIN"])
CROSS_BIN = TOOLCHAIN / "bin"
TRIPLE = "wasm64-submilli-linux-musl"


def fetch_crate(name: str, version: str, patches_dir: pathlib.Path) -> pathlib.Path:
    dst = patches_dir / f"{name}-{version}"
    if dst.is_dir():
        return dst
    patches_dir.mkdir(parents=True, exist_ok=True)
    archive = patches_dir / f"{name}-{version}.crate"
    url = f"https://static.crates.io/crates/{name}/{name}-{version}.crate"
    print(f"Downloading {url}", flush=True)
    urllib.request.urlretrieve(url, archive)
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(patches_dir)
    archive.unlink(missing_ok=True)
    if not dst.is_dir():
        raise SystemExit(f"Expected crate directory {dst} after extracting {name}-{version}")
    return dst


def prepare_crate_patches(patches_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    # 1. linux-raw-sys: map wasm64 to asm-generic LP64 aarch64 Linux UAPI bindings
    lrs = fetch_crate("linux-raw-sys", "0.12.1", patches_dir)
    lrs_lib = lrs / "src/lib.rs"
    lrs_text = lrs_lib.read_text(encoding="utf-8")
    if 'target_arch = "wasm64"' not in lrs_text:
        new_text = lrs_text.replace(
            'target_arch = "aarch64"',
            'any(target_arch = "aarch64", target_arch = "wasm64")',
        )
        if new_text == lrs_text:
            raise SystemExit("Failed to patch linux-raw-sys-0.12.1")
        lrs_lib.write_text(new_text, encoding="utf-8")

    # 2. rustix: include wasm64 in asm-generic Linux ioctl direction constants
    # and skip duplicate statx_flags re-exports already provided by modern libc.
    rustix = fetch_crate("rustix", "1.1.6", patches_dir)
    ioctl = rustix / "src/ioctl/linux.rs"
    ioctl_text = ioctl.read_text(encoding="utf-8")
    if 'target_arch = "wasm64"' not in ioctl_text:
        new_text = ioctl_text.replace(
            'target_arch = "csky"',
            'target_arch = "csky",\n    target_arch = "wasm64"',
        )
        if new_text == ioctl_text:
            raise SystemExit("Failed to patch rustix-1.1.6 ioctl")
        ioctl.write_text(new_text, encoding="utf-8")
    rustix_c = rustix / "src/backend/libc/c.rs"
    rustix_c_text = rustix_c.read_text(encoding="utf-8")
    if 'target_arch = "wasm64"' not in rustix_c_text:
        new_c = rustix_c_text.replace(
            'all(target_arch = "loongarch64", target_env = "musl")',
            'all(target_arch = "loongarch64", target_env = "musl"),\n        all(target_arch = "wasm64", target_env = "musl")',
        )
        if new_c != rustix_c_text:
            rustix_c.write_text(new_c, encoding="utf-8")

    return lrs, rustix


def main() -> None:
    patches_dir = BUILD / "crate-patches"
    lrs_dir, rustix_dir = prepare_crate_patches(patches_dir)

    target_dir = BUILD / "target"
    target_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["CARGO_PROFILE_RELEASE_OPT_LEVEL"] = "s"
    env["CARGO_PROFILE_RELEASE_LTO"] = "thin"

    cargo_wasmos = TOOLCHAIN / "rust/cargo-wasmos.py"
    cmd = [
        sys.executable,
        str(cargo_wasmos),
        "--toolchain",
        str(TOOLCHAIN),
        "--",
        "build",
        "--release",
        "--no-default-features",
        "--features",
        "completions",
        "--manifest-path",
        str(SRC / "Cargo.toml"),
        "--target-dir",
        str(target_dir),
        "--config",
        f'patch.crates-io.linux-raw-sys.path="{lrs_dir}"',
        "--config",
        f'patch.crates-io.rustix.path="{rustix_dir}"',
    ]
    subprocess.run(cmd, env=env, check=True)

    built_bin = target_dir / TRIPLE / "release/fd"
    if not built_bin.is_file():
        raise SystemExit(f"Expected built binary not found: {built_bin}")

    out_bin = DESTDIR / "usr/bin/fd"
    out_bin.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built_bin, out_bin)
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
    print(f"Built fd-10.5.2 -> {out_bin} ({out_bin.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
