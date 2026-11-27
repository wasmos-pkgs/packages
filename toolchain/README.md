# Toolchain (`wasm64-submilli-linux-musl`)

This folder builds the C/C++ cross-compiler that turns normal Linux programs (like BusyBox, ncurses, and Bash) into 64-bit WebAssembly binaries for [`wasm-os`](https://github.com/submilli/wasm-os).

> This is a temporary home for the toolchain until the LLVM fork moves into its own repository under `submilli`.

## What's Inside

- **`patches/llvm-21.1.8-submilli.patch`** — Adds our target (`wasm64-submilli-linux-musl`) to Clang/LLVM 21.1.8. It tells C/C++ code "you are compiling for 64-bit Linux" (so standard Linux code compiles unchanged), while generating 64-bit WebAssembly (`memory64`) under the hood.
- **`musl-arch/` & `syscalls/`** — Our port of `musl` (a small Linux C standard library providing functions like `printf`, `malloc`, and `open`) so its OS calls talk directly to `wasm-os` via typed WebAssembly imports (`wali.SYS_*`).
- **`postlink/` (`wasmos-postlink`)** — A small Rust tool that runs automatically after linking. WebAssembly doesn't have a built-in way to copy a running call stack for Linux's `fork()`, so `wasmos-postlink` rewrites *only* the functions that can reach `fork()` to save and restore their state cleanly (programs that don't use `fork()` aren't touched).
- **`toolchain.toml` & `build.py`** — Pinned upstream versions (LLVM 21.1.8, `musl` 1.2.5, Linux 6.12 headers) and the script that fetches them, builds everything, and packs a ready-to-use `toolchain.tar.zst` archive.

## How It Works in CI

Compiling LLVM from scratch takes ~15–20 minutes, so we don't rebuild it for every package:

1. When anything in `toolchain/` changes, `.github/workflows/ci.yml` runs `toolchain/build.py` and uploads the finished archive to `ghcr.io/wasmos-pkgs/toolchain:latest` before any package jobs run.
2. Package builds simply download and extract that archive in a few seconds and immediately start compiling packages with `wasm64-submilli-linux-musl-clang`.

## Building Locally

```bash
python3 toolchain/build.py --out dist/toolchain.tar.zst
```
