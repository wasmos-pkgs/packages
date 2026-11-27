#!/usr/bin/env python3
"""Create a patched Rust sysroot overlay adding `wasm64-submilli-linux-musl` support
to `std` and vendored `libc` (6-line surgical patch, zero `rustc` fork required).
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess


def patch_once(path: pathlib.Path, marker: str, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return
    if old not in text:
        raise RuntimeError(f"Expected snippet not found in {path}:\n{old}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def patch_libc_dir(libc_dir: pathlib.Path) -> None:
    # 1. Route wasm64 to b64 module in linux/musl/mod.rs
    patch_once(
        libc_dir / "src/unix/linux_like/linux/musl/mod.rs",
        'target_arch = "wasm64"',
        '        target_arch = "wasm32",',
        '        target_arch = "wasm32",\n        target_arch = "wasm64",',
    )
    # 2. Reuse aarch64 (asm-generic LP64) musl b64 definitions for wasm64
    patch_once(
        libc_dir / "src/unix/linux_like/linux/musl/b64/mod.rs",
        'target_arch = "wasm64"',
        '    if #[cfg(target_arch = "aarch64")] {',
        '    if #[cfg(any(target_arch = "aarch64", target_arch = "wasm64"))] {',
    )
    # 3. pthread_mutexattr_t alignment = 4 on wasm64 (same as aarch64/x86_64/riscv64)
    linux_mod = libc_dir / "src/unix/linux_like/linux/mod.rs"
    text = linux_mod.read_text(encoding="utf-8")
    if 'target_arch = "wasm64"' not in text:
        old = '            target_arch = "loongarch64"'
        new = '            target_arch = "loongarch64",\n            target_arch = "wasm64"'
        count = text.count(old)
        if count < 2:
            raise RuntimeError(f"Expected >= 2 loongarch64 matches in {linux_mod}, found {count}")
        linux_mod.write_text(text.replace(old, new), encoding="utf-8")
    # 4. Always enable musl_v1_2_3 ABI on wasm64 (musl 1.2.5+)
    patch_once(
        libc_dir / "build.rs",
        'target_arch == "wasm64"',
        '    if target_arch == "loongarch64" || target_arch == "hexagon" || target_env == "ohos" {',
        '    if target_arch == "loongarch64" || target_arch == "hexagon" || target_arch == "wasm64" || target_env == "ohos" {',
    )


def patch_library_dir(lib_dir: pathlib.Path) -> pathlib.Path:
    # 1. stdarch/core_arch: expose wasm32/wasm64 intrinsics even when target_family = ["unix"]
    patch_once(
        lib_dir / "stdarch/crates/core_arch/src/mod.rs",
        'target_family = "wasm", target_arch = "wasm32", target_arch = "wasm64"',
        '#[cfg(any(target_family = "wasm", doc))]\n#[doc(cfg(target_family = "wasm"))]\nmod wasm32;',
        '#[cfg(any(target_family = "wasm", target_arch = "wasm32", target_arch = "wasm64", doc))]\n#[doc(cfg(target_family = "wasm"))]\nmod wasm32;',
    )
    # 2. std/src/os/linux/raw.rs: wasm64 uses the asm-generic (aarch64/riscv64) stat layout
    patch_once(
        lib_dir / "std/src/os/linux/raw.rs",
        'target_arch = "wasm64"',
        '    target_arch = "aarch64",',
        '    target_arch = "aarch64",\n    target_arch = "wasm64",',
    )
    # 3. unwind/src/lib.rs: panic=abort on wasm64 does not link libunwind.a
    patch_once(
        lib_dir / "unwind/src/lib.rs",
        'target_arch = "wasm64"',
        'target_os = "espidf",',
        'target_os = "espidf",\n        target_arch = "wasm64",',
    )
    patch_once(
        lib_dir / "unwind/src/lib.rs",
        'not(target_arch = "wasm64")',
        '#[cfg(target_env = "musl")]',
        '#[cfg(all(target_env = "musl", not(target_arch = "wasm64")))]',
    )
    # 4. panic_unwind/src/lib.rs: route wasm64 to dummy.rs while panic=abort is active
    patch_once(
        lib_dir / "panic_unwind/src/lib.rs",
        'target_arch = "wasm64"',
        'target_os = "espidf"',
        'target_os = "espidf", target_arch = "wasm64"',
    )
    # 5. std/src/sys/personality/mod.rs: wasm64 uses Wasm EH personality stub (not libunwind gcc.rs)
    patch_once(
        lib_dir / "std/src/sys/personality/mod.rs",
        'target_arch = "wasm64"',
        'target_family = "wasm"',
        'target_family = "wasm", target_arch = "wasm64"',
    )
    # 6. backtrace: wasm64 uses noop backtrace (no native libunwind _Unwind_Backtrace)
    patch_once(
        lib_dir / "backtrace/src/backtrace/mod.rs",
        'not(target_arch = "wasm64")',
        'not(target_os = "emscripten"),',
        'not(target_os = "emscripten"),\n                not(target_arch = "wasm64"),',
    )
    # 7. compiler-builtins: wasm64 float comparison libcalls return i32 (matching LLVM WebAssembly ABI)
    patch_once(
        lib_dir / "compiler-builtins/compiler-builtins/src/float/cmp.rs",
        'target_arch = "wasm64"',
        'target_arch = "arm64ec", target_family = "wasm"',
        'target_arch = "arm64ec", target_family = "wasm", target_arch = "wasm64"',
    )

    # 2. Patch vendored libc inside rust-src
    libc_dirs = sorted((lib_dir / "vendor").glob("libc-*"))
    if not libc_dirs:
        raise RuntimeError(f"No vendored libc-* directory found under {lib_dir / 'vendor'}")
    for d in libc_dirs:
        patch_libc_dir(d)
    return libc_dirs[0]


def prepare_patched_sysroot(out_sysroot: pathlib.Path, rustc: str = "rustc") -> tuple[pathlib.Path, pathlib.Path]:
    host_sysroot = pathlib.Path(
        subprocess.check_output([rustc, "--print", "sysroot"], text=True).strip()
    )
    src_rust = host_sysroot / "lib/rustlib/src/rust"
    if not (src_rust / "library/std/src/lib.rs").is_file():
        raise SystemExit(
            f"Missing rust-src at {src_rust}. Run: rustup component add rust-src"
        )

    out_sysroot = out_sysroot.resolve()
    marker = out_sysroot / ".wasmos-rust-src-ready"
    rustc_ver = subprocess.check_output([rustc, "--version"], text=True).strip()
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == rustc_ver:
        libc_dirs = sorted((out_sysroot / "lib/rustlib/src/rust/library/vendor").glob("libc-*"))
        return out_sysroot, libc_dirs[0]

    if out_sysroot.exists():
        shutil.rmtree(out_sysroot)
    (out_sysroot / "lib/rustlib/src/rust").mkdir(parents=True, exist_ok=True)

    # Symlink bin/ and host target lib/rustlib/<host> from the host sysroot
    for entry in host_sysroot.iterdir():
        if entry.name == "lib":
            continue
        os.symlink(entry, out_sysroot / entry.name)
    for entry in (host_sysroot / "lib").iterdir():
        if entry.name == "rustlib":
            continue
        os.symlink(entry, out_sysroot / "lib" / entry.name)
    for entry in (host_sysroot / "lib/rustlib").iterdir():
        if entry.name == "src":
            continue
        os.symlink(entry, out_sysroot / "lib/rustlib" / entry.name)

    # Copy library/ so we can apply our 6-line wasm64 patch cleanly without touching rustup's files
    dst_lib = out_sysroot / "lib/rustlib/src/rust/library"
    shutil.copytree(src_rust / "library", dst_lib, symlinks=True)
    patched_libc = patch_library_dir(dst_lib)

    marker.write_text(rustc_ver + "\n", encoding="utf-8")
    print(f"Prepared patched Rust sysroot at {out_sysroot} ({rustc_ver})", flush=True)
    return out_sysroot, patched_libc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=pathlib.Path, required=True, help="Output sysroot directory")
    parser.add_argument("--rustc", default="rustc")
    args = parser.parse_args()
    sysroot, libc_dir = prepare_patched_sysroot(args.out, args.rustc)
    print(f"SYSROOT={sysroot}")
    print(f"LIBC_DIR={libc_dir}")


if __name__ == "__main__":
    main()
