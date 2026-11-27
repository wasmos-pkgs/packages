# wasmos-pkgs/packages

Package build recipes and OCI binary registry for **`wasm64-submilli-linux-musl`** ([`wasm-os`](https://github.com/submilli/wasm-os)).

Every package is cross-compiled in GitHub Actions and published to GitHub Container Registry (`ghcr.io/wasmos-pkgs/<name>:<version>-r<rev>`) as a `linux/wasm64` OCI v1.1 artifact.

## Registry Layout (`ghcr.io/wasmos-pkgs/*`)

| Artifact | Tags | Contents |
| :--- | :--- | :--- |
| `ghcr.io/wasmos-pkgs/toolchain` | `0.1.0`, `0.1.0-<sha>`, `latest` | Relocatable `wasm64-submilli-linux-musl` Clang/LLVM 21.1.8 + `musl` 1.2.5 + `compiler-rt` + `wasmos-postlink` tarball (`.tar.zst`) |
| `ghcr.io/wasmos-pkgs/busybox` | `1.37.0-r0`, `1.37.0`, `latest` | `/bin/busybox` multi-call Wasm64 binary |
| `ghcr.io/wasmos-pkgs/ncurses` | `6.5-r0`, `6.5`, `latest` | `/usr/include/*` and `/usr/lib/libncursesw.a` (+ `ncurses`/`tinfo`/`termcap`/`curses` aliases) |
| `ghcr.io/wasmos-pkgs/bash` | `5.3-r0`, `5.3`, `latest` | `/bin/bash` (GNU Bash 5.3 + Readline 8.3 statically linked) |
| `ghcr.io/wasmos-pkgs/index` | `latest` | `index.json` catalog of all packages and pinned versions |

### Using Packages in Derived Dockerfiles

Because every package is a standard `linux/wasm64` OCI image layer, you can compose images directly with `COPY --from=`:

```dockerfile
FROM ghcr.io/submilli/wasm-os:latest
COPY --from=ghcr.io/wasmos-pkgs/bash:5.3-r0 / /
```

## Repository Structure

```text
├── toolchain/                  # Temporary home for LLVM 21.1.8 patch, musl arch/wasm64, and wasmos-postlink
│   ├── toolchain.toml          # Pinned LLVM / musl / Linux UAPI versions
│   ├── build.py                # Builds relocatable toolchain.tar.zst (no Docker)
│   ├── patches/
│   ├── musl-arch/
│   └── postlink/
├── packages/
│   ├── busybox/                # pkg.toml (version, source, deps) + build.py
│   ├── ncurses/
│   └── bash/
├── scripts/
│   ├── plan-ci.py              # Diff + static-dep DAG -> parallel CI waves
│   ├── build-pkg.py            # Fetches deps/source, builds, verifies \0asm magic, packs & pushes
│   └── oci.py                  # Zero-dependency OCI Distribution v2 push/pull/index client
└── .github/workflows/
    ├── build-toolchain.yml     # Runs only on toolchain/** changes
    └── build-packages.yml      # Runs only changed packages + transitive dependents in DAG waves
```

## How CI Works

1. **Toolchain (`build-toolchain.yml`):** Triggers only when `toolchain/**` changes. Compiles LLVM 21.1.8 + `musl` + `compiler-rt` + `wasmos-postlink` directly on `ubuntu-latest` (with `ccache`), packs `toolchain.tar.zst`, and publishes `ghcr.io/wasmos-pkgs/toolchain:latest`.
2. **Packages (`build-packages.yml`):** Triggers only when `packages/**` or `scripts/**` changes (or manually via `workflow_dispatch`).
   - `scripts/plan-ci.py` inspects the git diff, expands any modified library to its transitive `build_deps` dependents (e.g. changing `ncurses` automatically rebuilds `bash`), and groups builds into topological waves.
   - Each job pulls `ghcr.io/wasmos-pkgs/toolchain:latest` in seconds, builds only its assigned package, verifies every executable has `\0asm` magic, and on `main` pushes `:<version>-r<rev>`, `:<version>`, and `:latest` plus updates `ghcr.io/wasmos-pkgs/index:latest`.

## Local Usage

```bash
# Inspect what CI would build for a package
python3 scripts/plan-ci.py --packages ncurses

# Build a package locally using an existing toolchain directory
python3 scripts/build-pkg.py busybox --toolchain /path/to/wasmos/toolchain
```
