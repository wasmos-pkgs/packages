# wasmos-pkgs/packages

Package build recipes, cross-compilation toolchain builder, and OCI binary registry for **`wasm64-submilli-linux-musl`** ([`submilli/wasm-os`](https://github.com/submilli/wasm-os)).

Every package is cross-compiled in GitHub Actions into native 64-bit WebAssembly (`memory64`, Wasm 3.0) and published to GitHub Container Registry (`ghcr.io/wasmos-pkgs/*`) as a standard `linux/wasm64` OCI v1.1 image layer.

---

## How It Works

```text
1. Toolchain CI (triggers ONLY on toolchain/**)
   toolchain/build.py (Ubuntu runner + ccache, no Docker)
     ├─ builds wasmos-postlink (fork unwind/rewind rewriter)
     ├─ patches & builds LLVM/Clang/LLD 21.1.8 (WebAssembly target)
     ├─ installs Linux 6.12 UAPI headers + musl 1.2.5 (arch/wasm64) + compiler-rt
     └─ pushes relocatable tarball ──▶ ghcr.io/wasmos-pkgs/toolchain:<ver>, :latest

2. Package CI (triggers ONLY on packages/** or scripts/**)
   scripts/plan-ci.py (git diff + static build_deps DAG)
     ├─ computes ONLY changed packages + transitive static-lib dependents
     ├─ groups them into parallel topological waves (wave1 → wave2 → wave3 → wave4)
     └─ each matrix job:
          ├─ pulls ghcr.io/wasmos-pkgs/toolchain:latest (~5s, zero LLVM rebuild)
          ├─ pulls required build_deps from GHCR into $WASMOS_SYSROOT
          ├─ downloads & SHA-256 verifies upstream source
          ├─ runs packages/<name>/build.py
          ├─ verifies every executable has `\0asm` magic & embeds pkg metadata
          └─ pushes ──▶ ghcr.io/wasmos-pkgs/<name>:<ver>-r<rev>, :<ver>, :latest

3. Index Update
   scripts/oci.py publish-index
     └─ publishes catalog of all packages & versions ──▶ ghcr.io/wasmos-pkgs/index:latest
```

---

## What's Published on GHCR

| Artifact | Tags | Kind | Contents |
| :--- | :--- | :--- | :--- |
| `ghcr.io/wasmos-pkgs/toolchain` | `0.1.0`, `0.1.0-<sha>`, `latest` | Host toolchain (`linux/amd64`) | Relocatable `wasm64-submilli-linux-musl-*` compilers, `wasmos-postlink`, and `musl` sysroot (`.tar.zst`) |
| `ghcr.io/wasmos-pkgs/busybox` | `1.37.0-r0`, `1.37.0`, `latest` | `runtime` (`linux/wasm64`) | `/bin/busybox` multi-call Wasm64 binary (`ash` + 120+ UNIX tools) |
| `ghcr.io/wasmos-pkgs/ncurses` | `6.5-r0`, `6.5`, `latest` | `dev` (`linux/wasm64`) | `/usr/include/*` and `/usr/lib/libncursesw.a` (+ `ncurses`, `tinfo`, `termcap`, `curses` aliases) |
| `ghcr.io/wasmos-pkgs/bash` | `5.3-r0`, `5.3`, `latest` | `runtime` (`linux/wasm64`) | `/bin/bash` (GNU Bash 5.3 + Readline 8.3 statically linked) |
| `ghcr.io/wasmos-pkgs/index` | `latest` | Catalog (`linux/wasm64`) | `index.json` listing every package, version, dependencies, and GHCR image ref |

Every published package archive also includes `/usr/share/wasmos-pkg/<name>.json` containing its exact version, revision, dependencies, license, and installed file list.

---

## Consuming Packages

### 1. Composing Derived Docker Images (`COPY --from=`)
Because every package is a standard `linux/wasm64` OCI v1.1 image, Docker can layer packages directly onto a `wasm-os` base image without needing a package manager:

```dockerfile
FROM ghcr.io/submilli/wasm-os:latest
COPY --from=ghcr.io/wasmos-pkgs/bash:5.3-r0 / /
```

```bash
docker build --platform linux/wasm64 -t my-wasmos-image:latest .
```

### 2. Pulling Packages or the Index Directly
Use `scripts/oci.py` (zero external Python dependencies) or any OCI client (`oras`, `crane`, `skopeo`):

```bash
# Pull bash 5.3-r0 into ./out
python3 scripts/oci.py pull --repo wasmos-pkgs/bash --tag 5.3-r0 --dest ./out

# Pull the package catalog
python3 scripts/oci.py pull --repo wasmos-pkgs/index --tag latest --dest ./idx
cat ./idx/index.json
```

---

## Repository Layout

```text
├── toolchain/                        # Temporary home until LLVM fork moves to submilli org
│   ├── toolchain.toml                # Pinned LLVM (21.1.8), musl (1.2.5), Linux (6.12) versions
│   ├── build.py                      # Builds relocatable toolchain.tar.zst directly on host
│   ├── patches/                      # llvm-21.1.8-submilli.patch
│   ├── musl-arch/                    # musl arch/wasm64 port + syscall/setjmp overrides
│   ├── syscalls/                     # Canonical wali.SYS_* generator
│   └── postlink/                     # Standalone Rust workspace for wasmos-postlink
├── packages/
│   ├── busybox/                      # pkg.toml + build.py
│   ├── ncurses/                      # pkg.toml + build.py
│   └── bash/                         # pkg.toml (build_deps = ["ncurses"]) + build.py
├── scripts/
│   ├── plan-ci.py                    # Computes changed packages + transitive DAG waves
│   ├── build-pkg.py                  # Unified package builder, verifier, packager & pusher
│   ├── oci.py                        # Zero-dependency OCI Distribution v2 push/pull/index client
│   └── toml_loader.py                # Python 3.9+ / 3.11+ tomllib compatibility wrapper
└── .github/workflows/
    ├── build-toolchain.yml           # Runs only on toolchain/** changes
    └── build-packages.yml            # Runs only changed packages + dependents in DAG waves
```

---

## Adding a New Package

Create `packages/<name>/pkg.toml` and `packages/<name>/build.py`:

### 1. `packages/<name>/pkg.toml`
```toml
[package]
name = "zlib"
version = "1.3.1"
rev = 0                       # Increment rev for recipe/rebuild changes of the same upstream version
kind = "dev"                  # "runtime" (executables) or "dev" (headers + static .a libraries)
description = "zlib compression library for wasm64-submilli-linux-musl"
license = "Zlib"
homepage = "https://zlib.net/"
build_deps = []               # Other packages under packages/ to install into $WASMOS_SYSROOT before building
runtime_deps = []

[source]
url = "https://zlib.net/zlib-1.3.1.tar.gz"
sha256 = "<64-char-hex>"
strip_components = 1
# Or pin a git commit:
# git = "https://..."
# rev = "<40-char-sha>"
# branch = "main"
```

### 2. `packages/<name>/build.py`
`scripts/build-pkg.py` invokes `packages/<name>/build.py` with these environment variables:

| Variable | Meaning |
| :--- | :--- |
| `WASMOS_TOOLCHAIN` | Root of the extracted toolchain (`$WASMOS_TOOLCHAIN/bin/wasm64-submilli-linux-musl-clang`, etc.) |
| `WASMOS_SYSROOT` | Toolchain sysroot (`include/` and `lib/`), pre-populated with all `build_deps` |
| `WASMOS_SRC` | Unpacked and verified upstream source directory |
| `WASMOS_BUILD` | Clean out-of-tree build directory |
| `WASMOS_DESTDIR` | Staging rootfs (`bin/<prog>` for runtime binaries; `usr/include` + `usr/lib` for libraries) |
| `WASMOS_JOBS` | Parallel make job count |

> **Important:** When stripping Wasm binaries, always preserve the `submilli.target` custom section:
> `wasm64-submilli-linux-musl-strip --strip-debug --keep-section=submilli.target <binary>`

---

## Selective CI & Local Commands

- **Path isolation:** Editing `README.md` triggers no workflows. Editing `packages/bash/*` rebuilds **only** `bash`. Editing `packages/ncurses/*` rebuilds `ncurses` in Wave 1 and automatically cascades to `bash` in Wave 2 (because `bash` links `libncursesw.a` statically).
- **Pull requests:** Build and verify all affected packages (passing Wave 1 artifacts to Wave 2 via GitHub Actions artifacts) without pushing to GHCR.
- **Manual rebuilds:** Trigger `Build Packages` via `workflow_dispatch` with `packages: "bash"` (or `"all"`).

```bash
# Preview which waves CI will build for a package
python3 scripts/plan-ci.py --packages ncurses

# Build a single package locally using a local toolchain directory
python3 scripts/build-pkg.py busybox --toolchain /path/to/wasmos/toolchain

# Dry-run package index generation
python3 scripts/oci.py publish-index --dry-run
```
