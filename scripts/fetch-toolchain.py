#!/usr/bin/env python3
"""Download, SHA-256 verify, and assemble the pinned WasmOS toolchain from
`wasmos-pkgs/toolchain` GitHub Releases (configured in `toolchain.toml`).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import urllib.request

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import toml_loader as tomllib  # noqa: E402

TRIPLE = "wasm64-submilli-linux-musl"


def detect_host() -> str:
    sys_name = platform.system().lower()
    mach = platform.machine().lower()
    os_part = {"linux": "linux", "darwin": "darwin", "windows": "windows"}.get(sys_name, sys_name)
    arch_part = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(mach, mach)
    return f"{os_part}-{arch_part}"


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def download_url(url: str, dest: pathlib.Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url)
    tok = (
        os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GHCR_TOKEN")
        or ""
    )
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=300) as resp, tmp.open("wb") as f:
        shutil.copyfileobj(resp, f)
    tmp.replace(dest)


def fetch_and_extract(
    repo: str,
    tag: str,
    asset: str,
    cache_dir: pathlib.Path,
    dest_dir: pathlib.Path,
) -> None:
    base = f"https://github.com/{repo}/releases/download/{tag}"
    sums_path = cache_dir / f"{tag}-SHA256SUMS.txt"
    if not sums_path.is_file():
        print(f"Downloading {tag}-SHA256SUMS.txt", flush=True)
        download_url(f"{base}/{tag}-SHA256SUMS.txt", sums_path)

    expected = ""
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == asset:
            expected = parts[0].lower()
            break
    if not expected:
        raise SystemExit(f"Asset '{asset}' not found in {sums_path}")

    archive = cache_dir / asset
    if not archive.is_file() or sha256_file(archive) != expected:
        print(f"Downloading {base}/{asset}", flush=True)
        download_url(f"{base}/{asset}", archive)
        actual = sha256_file(archive)
        if actual != expected:
            archive.unlink(missing_ok=True)
            raise SystemExit(
                f"SHA-256 mismatch for {asset}: expected {expected}, got {actual}"
            )
    else:
        print(f"Using cached {asset} (SHA-256 OK)", flush=True)

    dest_dir.mkdir(parents=True, exist_ok=True)
    zstd_proc = subprocess.Popen(["zstd", "-dc", str(archive)], stdout=subprocess.PIPE)
    assert zstd_proc.stdout is not None
    tar_proc = subprocess.Popen(["tar", "-xf", "-", "-C", str(dest_dir)], stdin=zstd_proc.stdout)
    zstd_proc.stdout.close()
    tar_rc = tar_proc.wait()
    zstd_rc = zstd_proc.wait()
    if tar_rc != 0 or zstd_rc != 0:
        raise SystemExit(f"Failed to extract {archive} (zstd={zstd_rc}, tar={tar_rc})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch pinned WasmOS toolchain releases")
    parser.add_argument("--manifest", type=pathlib.Path, default=REPO_ROOT / "toolchain.toml")
    parser.add_argument("--dest", type=pathlib.Path, required=True)
    parser.add_argument("--cache-dir", type=pathlib.Path, default=REPO_ROOT / "build/_toolchain-cache")
    parser.add_argument("--host", default=detect_host())
    args = parser.parse_args()

    cfg = tomllib.loads(args.manifest.read_text(encoding="utf-8"))["toolchain"]
    repo = str(cfg["repo"])
    postlink_tag = str(cfg["postlink"])
    llvm_tag = str(cfg["llvm"])
    musl_tag = str(cfg["musl"])
    libcxx_tag = str(cfg.get("libcxx", ""))
    rustc_tag = str(cfg["rustc"])

    dest = args.dest.resolve()
    cache_dir = args.cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    hid = args.host
    fetch_and_extract(repo, postlink_tag, f"{postlink_tag}-{hid}.tar.zst", cache_dir, dest)
    fetch_and_extract(repo, llvm_tag, f"{llvm_tag}-{hid}.tar.zst", cache_dir, dest)
    fetch_and_extract(repo, musl_tag, f"{musl_tag}-wasm64.tar.zst", cache_dir, dest)
    if libcxx_tag:
        fetch_and_extract(repo, libcxx_tag, f"{libcxx_tag}-wasm64.tar.zst", cache_dir, dest)
    fetch_and_extract(repo, rustc_tag, f"{rustc_tag}-{hid}.tar.zst", cache_dir, dest / "rustc")

    # Place wasmos-postlink beside clang so Clang locates it via GetProgramPath
    postlink_src = dest / "bin/wasmos-postlink"
    postlink_dst = dest / "llvm/bin/wasmos-postlink"
    if not postlink_src.is_file():
        raise SystemExit(f"Missing {postlink_src}")
    shutil.copy2(postlink_src, postlink_dst)
    postlink_dst.chmod(0o755)

    # Place compiler-rt builtins in Clang's resource directory
    builtins_a = dest / "sysroot/lib/libclang_rt.builtins-wasm64.a"
    if not builtins_a.is_file():
        raise SystemExit(f"Missing {builtins_a}")
    resource_dir = subprocess.check_output(
        [str(dest / "llvm/bin/clang"), f"--target={TRIPLE}", "-print-resource-dir"],
        text=True,
    ).strip()
    for dst in (
        pathlib.Path(resource_dir) / "lib/linux/libclang_rt.builtins-wasm64.a",
        pathlib.Path(resource_dir) / "lib/wasm64-unknown-linux-musl/libclang_rt.builtins.a",
    ):
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(builtins_a, dst)

    print(f"Assembled WasmOS toolchain ({hid}) -> {dest}", flush=True)


if __name__ == "__main__":
    main()
