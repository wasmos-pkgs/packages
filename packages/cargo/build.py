#!/usr/bin/env python3
"""Cross-compile `cargo` 1.94.0 for `wasm64-submilli-linux-musl`
-> `$WASMOS_DESTDIR/usr/bin/cargo`.
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
SYSROOT = pathlib.Path(os.environ["WASMOS_SYSROOT"])
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


def prepare_crate_patches(patches_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    # 1. linux-raw-sys: map wasm64 to identical asm-generic LP64 aarch64 Linux UAPI bindings
    lrs = fetch_crate("linux-raw-sys", "0.11.0", patches_dir)
    lrs_lib = lrs / "src/lib.rs"
    lrs_text = lrs_lib.read_text(encoding="utf-8")
    if 'target_arch = "wasm64"' not in lrs_text:
        new_text = lrs_text.replace(
            'target_arch = "aarch64"',
            'any(target_arch = "aarch64", target_arch = "wasm64")',
        )
        if new_text == lrs_text:
            raise SystemExit("Failed to patch linux-raw-sys-0.11.0")
        lrs_lib.write_text(new_text, encoding="utf-8")

    # 2. rustix: include wasm64 in asm-generic Linux ioctl direction constants
    rustix = fetch_crate("rustix", "1.1.3", patches_dir)
    ioctl = rustix / "src/ioctl/linux.rs"
    ioctl_text = ioctl.read_text(encoding="utf-8")
    if 'target_arch = "wasm64"' not in ioctl_text:
        new_text = ioctl_text.replace(
            'target_arch = "csky"',
            'target_arch = "csky",\n    target_arch = "wasm64"',
        )
        if new_text == ioctl_text:
            raise SystemExit("Failed to patch rustix-1.1.3")
        ioctl.write_text(new_text, encoding="utf-8")

    # 3. zlib-rs: opt into unstable simd_wasm64 when building with stock rustc + RUSTC_BOOTSTRAP=1
    zlib_rs = fetch_crate("zlib-rs", "0.5.5", patches_dir)
    zlib_lib = zlib_rs / "src/lib.rs"
    zlib_text = zlib_lib.read_text(encoding="utf-8")
    if "simd_wasm64" not in zlib_text:
        zlib_lib.write_text(
            '#![cfg_attr(target_arch = "wasm64", feature(simd_wasm64))]\n' + zlib_text,
            encoding="utf-8",
        )

    return lrs, rustix, zlib_rs


def main() -> None:
    for lib in ("libz.a", "libssl.a", "libcrypto.a", "libcurl.a", "libsqlite3.a"):
        if not (SYSROOT / "lib" / lib).is_file():
            raise SystemExit(f"Missing {SYSROOT / 'lib' / lib}; install build_deps first")

    # Ensure pkg-config prefix=/usr resolves cleanly under PKG_CONFIG_SYSROOT_DIR=$SYSROOT
    usr_link = SYSROOT / "usr"
    if not usr_link.exists() and not usr_link.is_symlink():
        usr_link.symlink_to(".")

    patches_dir = BUILD / "crate-patches"
    lrs_dir, rustix_dir, zlib_rs_dir = prepare_crate_patches(patches_dir)

    target_dir = BUILD / "target"
    target_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PKG_CONFIG_ALLOW_CROSS"] = "1"
    env["PKG_CONFIG_LIBDIR"] = str(SYSROOT / "lib/pkgconfig")
    env["PKG_CONFIG_SYSROOT_DIR"] = str(SYSROOT)
    env["OPENSSL_DIR"] = str(SYSROOT)
    env["OPENSSL_STATIC"] = "1"
    env["LIBSQLITE3_SYS_USE_PKG_CONFIG"] = "1"
    env["CARGO_PROFILE_RELEASE_OPT_LEVEL"] = "s"
    env["CARGO_PROFILE_RELEASE_LTO"] = "thin"
    extra_rf = env.get("RUSTFLAGS", "").strip()
    env["RUSTFLAGS"] = f"{extra_rf} -L native={SYSROOT / 'lib'}".strip()

    cargo_wasmos = TOOLCHAIN / "rust/cargo-wasmos.py"
    cmd = [
        sys.executable,
        str(cargo_wasmos),
        "--toolchain",
        str(TOOLCHAIN),
        "--",
        "build",
        "--release",
        "--bin",
        "cargo",
        "--manifest-path",
        str(SRC / "Cargo.toml"),
        "--target-dir",
        str(target_dir),
        "--config",
        f'patch.crates-io.linux-raw-sys.path="{lrs_dir}"',
        "--config",
        f'patch.crates-io.rustix.path="{rustix_dir}"',
        "--config",
        f'patch.crates-io.zlib-rs.path="{zlib_rs_dir}"',
    ]
    subprocess.run(cmd, env=env, check=True)

    built_bin = target_dir / TRIPLE / "release/cargo"
    if not built_bin.is_file():
        raise SystemExit(f"Expected built binary not found: {built_bin}")

    out_bin = DESTDIR / "usr/bin/cargo"
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
    print(f"Built cargo-1.94.0 -> {out_bin} ({out_bin.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
