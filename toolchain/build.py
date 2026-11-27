#!/usr/bin/env python3
"""Build the relocatable `wasm64-submilli-linux-musl` toolchain and pack it as a tarball.

Stages:
  1. Build `wasmos-postlink` (Rust fork unwind/rewind rewriter).
  2. Fetch LLVM 21.1.8, apply `llvm-21.1.8-submilli.patch`, build & install clang/lld/binutils.
  3. Install sanitized Linux 6.12 UAPI headers into `$PREFIX/sysroot/include`.
  4. Fetch musl 1.2.5, overlay `musl-arch/wasm64` + overrides, build & install `libc.a` + `crt*.o`.
  5. Build `compiler-rt` builtins into `$PREFIX/sysroot/lib` and Clang's resource dir.
  6. Generate relocatable `$PREFIX/bin/wasm64-submilli-linux-musl-*` wrappers.
  7. Archive `$PREFIX` to `dist/toolchain.tar.zst` (or `.tar.gz`).
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import toml_loader as tomllib  # noqa: E402

TOOLCHAIN_DIR = REPO_ROOT / "toolchain"
TRIPLE = "wasm64-submilli-linux-musl"

COMPILER_RT_SOURCES = [
    "absvdi2.c", "absvsi2.c", "absvti2.c",
    "adddf3.c", "addsf3.c", "addvdi3.c", "addvsi3.c", "addvti3.c",
    "ashldi3.c", "ashlti3.c", "ashrdi3.c", "ashrti3.c",
    "bswapdi2.c", "bswapsi2.c",
    "clzdi2.c", "clzsi2.c", "clzti2.c",
    "cmpdi2.c", "cmpti2.c", "comparedf2.c", "comparesf2.c",
    "ctzdi2.c", "ctzsi2.c", "ctzti2.c",
    "divdc3.c", "divdf3.c", "divdi3.c", "divmoddi4.c", "divmodsi4.c", "divmodti4.c",
    "divsc3.c", "divsf3.c", "divsi3.c", "divti3.c",
    "extendsfdf2.c", "extendhfsf2.c", "extendhfdf2.c",
    "ffsdi2.c", "ffssi2.c", "ffsti2.c",
    "fixdfdi.c", "fixdfsi.c", "fixdfti.c", "fixsfdi.c", "fixsfsi.c", "fixsfti.c",
    "fixunsdfdi.c", "fixunsdfsi.c", "fixunsdfti.c", "fixunssfdi.c", "fixunssfsi.c", "fixunssfti.c",
    "floatdidf.c", "floatdisf.c", "floatsidf.c", "floatsisf.c", "floattidf.c", "floattisf.c",
    "floatundidf.c", "floatundisf.c", "floatunsidf.c", "floatunsisf.c", "floatuntidf.c", "floatuntisf.c",
    "fp_mode.c", "int_util.c",
    "lshrdi3.c", "lshrti3.c",
    "moddi3.c", "modsi3.c", "modti3.c",
    "muldc3.c", "muldf3.c", "muldi3.c", "mulodi4.c", "mulosi4.c", "muloti4.c",
    "mulsc3.c", "mulsf3.c", "multi3.c", "mulvdi3.c", "mulvsi3.c", "mulvti3.c",
    "negdf2.c", "negdi2.c", "negsf2.c", "negti2.c", "negvdi2.c", "negvsi2.c", "negvti2.c",
    "paritydi2.c", "paritysi2.c", "parityti2.c",
    "popcountdi2.c", "popcountsi2.c", "popcountti2.c",
    "powidf2.c", "powisf2.c",
    "subdf3.c", "subsf3.c", "subvdi3.c", "subvsi3.c", "subvti3.c",
    "truncdfhf2.c", "truncdfsf2.c", "truncsfhf2.c",
    "ucmpdi2.c", "ucmpti2.c",
    "udivdi3.c", "udivmoddi4.c", "udivmodsi4.c", "udivmodti4.c", "udivsi3.c", "udivti3.c",
    "umoddi3.c", "umodsi3.c", "umodti3.c",
    # 128-bit IEEE quad long double
    "addtf3.c", "comparetf2.c", "divtc3.c", "divtf3.c",
    "extenddftf2.c", "extendhftf2.c", "extendsftf2.c",
    "fixtfdi.c", "fixtfsi.c", "fixtfti.c",
    "fixunstfdi.c", "fixunstfsi.c", "fixunstfti.c",
    "floatditf.c", "floatsitf.c", "floattitf.c",
    "floatunditf.c", "floatunsitf.c", "floatuntitf.c",
    "multc3.c", "multf3.c", "powitf2.c", "subtf3.c",
    "trunctfdf2.c", "trunctfhf2.c", "trunctfsf2.c",
]

RE_USER = re.compile(r"([\s(])(?:__user|__force|__iomem)(?=\s)")
RE_ATTR_CONST = re.compile(r"__attribute_const__(?=\s|$)")
RE_COMPILER_INC = re.compile(r"^#include <linux/compiler(?:|_types)\.h>.*$", re.MULTILINE)
RE_PACKED = re.compile(r"(^|[^a-zA-Z0-9])__packed([^a-zA-Z0-9_]|$)", re.MULTILINE)
RE_KW = re.compile(r"(^|[\s(])(inline|asm|volatile)(?=[\s(]|$)", re.MULTILINE)
RE_UAPI_GUARD = re.compile(r"^#(ifndef|define|endif\s*/\*)\s*_UAPI", re.MULTILINE)


def run(cmd: list[str], cwd: pathlib.Path | None = None, env: dict[str, str] | None = None) -> None:
    print(f"+ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def git_clone_tag(repo: str, tag: str, dst: pathlib.Path) -> None:
    if dst.exists():
        print(f"Reusing existing checkout at {dst}", flush=True)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--depth", "1", "--branch", tag, repo, str(dst)])


def strip_kernel_blocks(text: str) -> str:
    out: list[str] = []
    stack: list[tuple[bool, bool]] = []
    if_re = re.compile(r"^\s*#\s*(ifdef|ifndef|if|elif|else|endif)\b(.*)$")
    for line in text.splitlines(keepends=True):
        m = if_re.match(line)
        if m:
            directive, rest = m.group(1), m.group(2).strip()
            rest_clean = re.sub(r"//.*$", "", re.sub(r"/\*.*?\*/", "", rest)).strip()
            parent_keep = all(k for _, k in stack)
            if directive == "ifdef":
                if rest_clean == "__KERNEL__":
                    stack.append((True, False))
                    continue
                stack.append((False, parent_keep))
            elif directive == "ifndef":
                if rest_clean == "__KERNEL__":
                    stack.append((True, parent_keep))
                    continue
                stack.append((False, parent_keep))
            elif directive == "if":
                if rest_clean in ("defined(__KERNEL__)", "defined (__KERNEL__)"):
                    stack.append((True, False))
                    continue
                if rest_clean in ("!defined(__KERNEL__)", "!defined (__KERNEL__)"):
                    stack.append((True, parent_keep))
                    continue
                stack.append((False, parent_keep))
            elif directive == "else":
                if stack and stack[-1][0]:
                    _, prev_keep = stack[-1]
                    parent_outer = all(k for _, k in stack[:-1])
                    stack[-1] = (True, parent_outer and not prev_keep)
                    continue
            elif directive == "elif":
                if stack and stack[-1][0]:
                    stack[-1] = (False, parent_keep)
                    if parent_keep:
                        out.append(f"#if {rest}\n")
                    continue
            elif directive == "endif":
                if stack:
                    was_kernel, _ = stack.pop()
                    if was_kernel:
                        continue
        if all(k for _, k in stack):
            out.append(line)
    return "".join(out)


def sanitize_header(src: pathlib.Path) -> str:
    text = src.read_text(encoding="utf-8", errors="replace")
    text = RE_COMPILER_INC.sub("", text)
    text = RE_USER.sub(r"\1", text)
    text = RE_ATTR_CONST.sub("", text)
    text = RE_PACKED.sub(r"\1__attribute__((packed))\2", text)
    text = RE_KW.sub(r"\1__\2__", text)
    text = RE_UAPI_GUARD.sub(r"#\1 ", text)
    return strip_kernel_blocks(text)


def install_uapi_headers(linux_src: pathlib.Path, dest_inc: pathlib.Path) -> None:
    dest_inc.mkdir(parents=True, exist_ok=True)
    count = 0
    for src_root, dst_root in [
        (linux_src / "include/uapi", dest_inc),
        (linux_src / "arch/arm64/include/uapi/asm", dest_inc / "asm"),
    ]:
        for src in sorted(src_root.rglob("*.h")):
            dst = dst_root / src.relative_to(src_root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(sanitize_header(src), encoding="utf-8")
            count += 1

    asm_dir = dest_inc / "asm"
    asm_dir.mkdir(parents=True, exist_ok=True)
    for generic_hdr in sorted((dest_inc / "asm-generic").glob("*.h")):
        target = asm_dir / generic_hdr.name
        if not target.exists():
            target.write_text(f"#include <asm-generic/{generic_hdr.name}>\n", encoding="utf-8")
            count += 1

    version_h = dest_inc / "linux/version.h"
    version_h.parent.mkdir(parents=True, exist_ok=True)
    version_h.write_text(
        "#define LINUX_VERSION_CODE 396288\n"
        "#define KERNEL_VERSION(a,b,c) (((a) << 16) + ((b) << 8) + ((c) > 255 ? 255 : (c)))\n"
        "#define LINUX_VERSION_MAJOR 6\n"
        "#define LINUX_VERSION_PATCHLEVEL 12\n"
        "#define LINUX_VERSION_SUBLEVEL 0\n",
        encoding="utf-8",
    )
    compiler_h = dest_inc / "linux/compiler.h"
    compiler_h.write_text(
        "#ifndef _LINUX_COMPILER_H\n"
        "#define _LINUX_COMPILER_H\n"
        "#define __user\n"
        "#define __force\n"
        "#define __iomem\n"
        "#define __rcu\n"
        "#endif\n",
        encoding="utf-8",
    )
    print(f"Installed {count + 2} sanitized Linux 6.12 UAPI headers -> {dest_inc}", flush=True)


def copy_tree(src: pathlib.Path, dst: pathlib.Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in sorted(src.rglob("*")):
        if item.is_dir():
            continue
        target = dst / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def patch_once(path: pathlib.Path, marker: str, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return
    if old not in text:
        raise RuntimeError(f"Expected snippet not found in {path}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def apply_musl_port(musl_src: pathlib.Path) -> None:
    dst_arch = musl_src / "arch/wasm64"
    dst_bits = dst_arch / "bits"
    dst_bits.mkdir(parents=True, exist_ok=True)
    for bit_file in sorted((musl_src / "arch/aarch64/bits").glob("*")):
        if bit_file.is_file():
            shutil.copy2(bit_file, dst_bits / bit_file.name)

    copy_tree(TOOLCHAIN_DIR / "musl-arch/wasm64", dst_arch)
    copy_tree(TOOLCHAIN_DIR / "musl-arch/overrides", musl_src)

    patch_once(
        musl_src / "configure",
        "wasm64*) ARCH=wasm64 ;;",
        "s390x*) ARCH=s390x ;;",
        "s390x*) ARCH=s390x ;;\nwasm64*) ARCH=wasm64 ;;",
    )
    patch_once(
        musl_src / "include/setjmp.h",
        "__sigsetjmp_save",
        "#define setjmp setjmp",
        "#define setjmp setjmp\n"
        "#ifdef __wasm__\n"
        "#define _setjmp setjmp\n"
        "#define _longjmp longjmp\n"
        "#if defined(_POSIX_SOURCE) || defined(_POSIX_C_SOURCE) \\\n"
        " || defined(_XOPEN_SOURCE) || defined(_GNU_SOURCE) || defined(_BSD_SOURCE)\n"
        "int __sigsetjmp_save(sigjmp_buf, int);\n"
        "#define sigsetjmp(buf, save) (__sigsetjmp_save((buf), (save)), setjmp(buf))\n"
        "#endif\n"
        "#endif",
    )
    patch_once(
        musl_src / "src/internal/syscall.h",
        "__wasm_cp_check",
        "#define __syscall_cp0(n) (__syscall_cp)(n,0,0,0,0,0,0)\n"
        "#define __syscall_cp1(n,a) (__syscall_cp)(n,__scc(a),0,0,0,0,0)\n"
        "#define __syscall_cp2(n,a,b) (__syscall_cp)(n,__scc(a),__scc(b),0,0,0,0)\n"
        "#define __syscall_cp3(n,a,b,c) (__syscall_cp)(n,__scc(a),__scc(b),__scc(c),0,0,0)\n"
        "#define __syscall_cp4(n,a,b,c,d) (__syscall_cp)(n,__scc(a),__scc(b),__scc(c),__scc(d),0,0)\n"
        "#define __syscall_cp5(n,a,b,c,d,e) (__syscall_cp)(n,__scc(a),__scc(b),__scc(c),__scc(d),__scc(e),0)\n"
        "#define __syscall_cp6(n,a,b,c,d,e,f) (__syscall_cp)(n,__scc(a),__scc(b),__scc(c),__scc(d),__scc(e),__scc(f))",
        "#ifdef __wasm__\n"
        "hidden void __wasm_cp_check(void);\n"
        "hidden long __wasm_cp_finish(long);\n"
        "#define __syscall_cp0(n) (__wasm_cp_check(), __wasm_cp_finish(__syscall0(n)))\n"
        "#define __syscall_cp1(n,a) (__wasm_cp_check(), __wasm_cp_finish(__syscall1(n,a)))\n"
        "#define __syscall_cp2(n,a,b) (__wasm_cp_check(), __wasm_cp_finish(__syscall2(n,a,b)))\n"
        "#define __syscall_cp3(n,a,b,c) (__wasm_cp_check(), __wasm_cp_finish(__syscall3(n,a,b,c)))\n"
        "#define __syscall_cp4(n,a,b,c,d) (__wasm_cp_check(), __wasm_cp_finish(__syscall4(n,a,b,c,d)))\n"
        "#define __syscall_cp5(n,a,b,c,d,e) (__wasm_cp_check(), __wasm_cp_finish(__syscall5(n,a,b,c,d,e)))\n"
        "#define __syscall_cp6(n,a,b,c,d,e,f) (__wasm_cp_check(), __wasm_cp_finish(__syscall6(n,a,b,c,d,e,f)))\n"
        "#else\n"
        "#define __syscall_cp0(n) (__syscall_cp)(n,0,0,0,0,0,0)\n"
        "#define __syscall_cp1(n,a) (__syscall_cp)(n,__scc(a),0,0,0,0,0)\n"
        "#define __syscall_cp2(n,a,b) (__syscall_cp)(n,__scc(a),__scc(b),0,0,0,0)\n"
        "#define __syscall_cp3(n,a,b,c) (__syscall_cp)(n,__scc(a),__scc(b),__scc(c),0,0,0)\n"
        "#define __syscall_cp4(n,a,b,c,d) (__syscall_cp)(n,__scc(a),__scc(b),__scc(c),__scc(d),0,0)\n"
        "#define __syscall_cp5(n,a,b,c,d,e) (__syscall_cp)(n,__scc(a),__scc(b),__scc(c),__scc(d),__scc(e),0)\n"
        "#define __syscall_cp6(n,a,b,c,d,e,f) (__syscall_cp)(n,__scc(a),__scc(b),__scc(c),__scc(d),__scc(e),__scc(f))\n"
        "#endif",
    )


def create_relocatable_wrappers(prefix: pathlib.Path) -> None:
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    def write_exec(path: pathlib.Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    cc_script = f"""#!/bin/sh
DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
exec "$DIR/llvm/bin/clang" --target={TRIPLE} --sysroot="$DIR/sysroot" "$@"
"""
    cxx_script = f"""#!/bin/sh
DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
exec "$DIR/llvm/bin/clang++" --target={TRIPLE} --sysroot="$DIR/sysroot" "$@"
"""
    cpp_script = f"""#!/bin/sh
DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
exec "$DIR/llvm/bin/clang" --target={TRIPLE} --sysroot="$DIR/sysroot" -E "$@"
"""
    for name in ("clang", "gcc", "cc"):
        write_exec(bin_dir / f"{TRIPLE}-{name}", cc_script)
    for name in ("clang++", "g++", "c++"):
        write_exec(bin_dir / f"{TRIPLE}-{name}", cxx_script)
    write_exec(bin_dir / f"{TRIPLE}-cpp", cpp_script)

    for short, llvm_tool in {
        "ar": "llvm-ar",
        "ranlib": "llvm-ranlib",
        "nm": "llvm-nm",
        "strip": "llvm-strip",
        "objcopy": "llvm-objcopy",
        "objdump": "llvm-objdump",
        "ld": "wasm-ld",
    }.items():
        link = bin_dir / f"{TRIPLE}-{short}"
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(f"../llvm/bin/{llvm_tool}", link)


def build_compiler_rt(
    llvm_src: pathlib.Path,
    prefix: pathlib.Path,
    build_dir: pathlib.Path,
    jobs: int,
) -> None:
    builtins_dir = llvm_src / "compiler-rt/lib/builtins"
    clang = prefix / "llvm/bin/clang"
    llvm_ar = prefix / "llvm/bin/llvm-ar"
    llvm_ranlib = prefix / "llvm/bin/llvm-ranlib"
    sysroot = prefix / "sysroot"

    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)

    def compile_one(src_name: str) -> pathlib.Path:
        src = builtins_dir / src_name
        obj = build_dir / f"{src.stem}.o"
        subprocess.run(
            [
                str(clang),
                f"--target={TRIPLE}",
                f"--sysroot={sysroot}",
                "-O2",
                "-std=c11",
                "-fno-builtin",
                "-fvisibility=hidden",
                "-fomit-frame-pointer",
                "-mno-exception-handling",
                "-DVISIBILITY_HIDDEN",
                f"-I{builtins_dir}",
                "-c",
                str(src),
                "-o",
                str(obj),
            ],
            check=True,
        )
        return obj

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        objs = list(pool.map(compile_one, COMPILER_RT_SOURCES))

    archive = build_dir / "libclang_rt.builtins-wasm64.a"
    run([str(llvm_ar), "rcs", str(archive), *(str(o) for o in objs)])
    run([str(llvm_ranlib), str(archive)])

    resource_dir = subprocess.check_output(
        [str(clang), f"--target={TRIPLE}", "-print-resource-dir"],
        text=True,
    ).strip()

    for dst in [
        pathlib.Path(resource_dir) / "lib/linux/libclang_rt.builtins-wasm64.a",
        pathlib.Path(resource_dir) / "lib/wasm64-unknown-linux-musl/libclang_rt.builtins.a",
        sysroot / "lib/libclang_rt.builtins.a",
        sysroot / "lib/libclang_rt.builtins-wasm64.a",
    ]:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive, dst)
    print(f"Built compiler-rt ({len(objs)} objects)", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=pathlib.Path, default=REPO_ROOT / "build/toolchain")
    parser.add_argument("--prefix", type=pathlib.Path, default=REPO_ROOT / "dist/toolchain-root")
    parser.add_argument("--out", type=pathlib.Path, default=REPO_ROOT / "dist/toolchain.tar.zst")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args()

    cfg = tomllib.loads((TOOLCHAIN_DIR / "toolchain.toml").read_text(encoding="utf-8"))["toolchain"]
    work = args.work_dir.resolve()
    sources = work / "sources"
    prefix = args.prefix.resolve()
    if prefix.exists():
        shutil.rmtree(prefix)
    prefix.mkdir(parents=True, exist_ok=True)

    # 1. Build wasmos-postlink
    postlink_dir = TOOLCHAIN_DIR / "postlink"
    run(["cargo", "build", "--release", "-p", "wasmos-postlink"], cwd=postlink_dir)
    postlink_bin = postlink_dir / "target/release/wasmos-postlink"

    # 2. Fetch & patch LLVM 21.1.8
    llvm_src = sources / "llvm-project"
    git_clone_tag(cfg["llvm_repo"], cfg["llvm_tag"], llvm_src)
    patch_file = TOOLCHAIN_DIR / "patches/llvm-21.1.8-submilli.patch"
    check_applied = subprocess.run(
        ["git", "apply", "--reverse", "--check", str(patch_file)],
        cwd=llvm_src,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check_applied.returncode != 0:
        run(["git", "apply", str(patch_file)], cwd=llvm_src)

    llvm_build = work / "build-llvm"
    llvm_build.mkdir(parents=True, exist_ok=True)
    cmake_args = [
        "cmake",
        "-G", "Ninja",
        "-S", str(llvm_src / "llvm"),
        "-B", str(llvm_build),
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_INSTALL_PREFIX={prefix / 'llvm'}",
        "-DLLVM_ENABLE_PROJECTS=clang;lld",
        "-DLLVM_TARGETS_TO_BUILD=WebAssembly",
        "-DLLVM_ENABLE_ASSERTIONS=OFF",
        "-DLLVM_INCLUDE_TESTS=OFF",
        "-DLLVM_INCLUDE_BENCHMARKS=OFF",
        "-DLLVM_INCLUDE_EXAMPLES=OFF",
        "-DLLVM_INCLUDE_DOCS=OFF",
        "-DCLANG_INCLUDE_TESTS=OFF",
        "-DCLANG_INCLUDE_DOCS=OFF",
        "-DLLVM_ENABLE_TERMINFO=OFF",
        "-DLLVM_ENABLE_ZLIB=OFF",
        "-DLLVM_ENABLE_ZSTD=OFF",
    ]
    if shutil.which("ccache"):
        cmake_args.append("-DLLVM_CCACHE_BUILD=ON")
    if shutil.which("lld"):
        cmake_args.append("-DLLVM_USE_LINKER=lld")

    run(cmake_args)
    targets = [
        "clang",
        "lld",
        "llvm-ar",
        "llvm-ranlib",
        "llvm-nm",
        "llvm-strip",
        "llvm-objcopy",
        "llvm-objdump",
        "clang-resource-headers",
    ]
    run(["cmake", "--build", str(llvm_build), "-j", str(args.jobs), "--target", *targets])

    # Install only the binaries and resource headers we actually need (keeps artifact slim).
    llvm_bin = prefix / "llvm/bin"
    llvm_bin.mkdir(parents=True, exist_ok=True)
    for tool in ["clang-21", "lld", "llvm-ar", "llvm-nm", "llvm-objcopy", "llvm-objdump"]:
        src_bin = llvm_build / "bin" / tool
        if src_bin.exists():
            shutil.copy2(src_bin, llvm_bin / tool)
    for link_name, target_name in [
        ("clang", "clang-21"),
        ("clang++", "clang-21"),
        ("wasm-ld", "lld"),
        ("llvm-ranlib", "llvm-ar"),
        ("llvm-strip", "llvm-objcopy"),
    ]:
        link = llvm_bin / link_name
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(target_name, link)

    # Copy clang resource headers (`lib/clang/21/include`)
    clang_lib_src = llvm_build / "lib/clang"
    clang_lib_dst = prefix / "llvm/lib/clang"
    if clang_lib_src.is_dir():
        shutil.copytree(clang_lib_src, clang_lib_dst, dirs_exist_ok=True)

    # Install wasmos-postlink into both llvm/bin and bin so Clang always finds it via GetProgramPath.
    shutil.copy2(postlink_bin, llvm_bin / "wasmos-postlink")
    (llvm_bin / "wasmos-postlink").chmod(0o755)
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(postlink_bin, prefix / "bin/wasmos-postlink")
    (prefix / "bin/wasmos-postlink").chmod(0o755)

    create_relocatable_wrappers(prefix)

    # 3. Linux 6.12 UAPI headers
    linux_src = sources / "linux"
    git_clone_tag(cfg["linux_repo"], cfg["linux_tag"], linux_src)
    install_uapi_headers(linux_src, prefix / "sysroot/include")

    # 4. musl 1.2.5 (`arch/wasm64`)
    musl_src = sources / "musl"
    git_clone_tag(cfg["musl_repo"], cfg["musl_tag"], musl_src)
    apply_musl_port(musl_src)

    musl_build = work / "build-musl"
    if musl_build.exists():
        shutil.rmtree(musl_build)
    musl_build.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PATH"] = f"{prefix / 'bin'}:{env.get('PATH', '')}"
    env["CC"] = str(prefix / f"bin/{TRIPLE}-clang")
    env["AR"] = str(prefix / f"bin/{TRIPLE}-ar")
    env["RANLIB"] = str(prefix / f"bin/{TRIPLE}-ranlib")
    run(
        [
            "sh",
            str(musl_src / "configure"),
            f"--target={TRIPLE}",
            f"--prefix={prefix / 'sysroot'}",
            "--disable-shared",
            "--disable-wrapper",
        ],
        cwd=musl_build,
        env=env,
    )
    run(["make", f"-j{args.jobs}"], cwd=musl_build, env=env)
    run(["make", "install"], cwd=musl_build, env=env)

    # 5. compiler-rt builtins
    build_compiler_rt(llvm_src, prefix, work / "build-compiler-rt", args.jobs)

    # 6. Metadata
    meta = {
        "name": "toolchain",
        "version": cfg["version"],
        "triple": TRIPLE,
        "llvm_tag": cfg["llvm_tag"],
        "musl_tag": cfg["musl_tag"],
        "linux_tag": cfg["linux_tag"],
        "host_os": platform.system().lower(),
        "host_arch": platform.machine().lower(),
    }
    (prefix / "toolchain.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    # 7. Pack tarball
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.name.endswith(".tar.zst"):
        run(["tar", "--zstd", "-cf", str(args.out), "-C", str(prefix), "."])
    else:
        run(["tar", "-czf", str(args.out), "-C", str(prefix), "."])
    print(f"Packed toolchain -> {args.out} ({args.out.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
