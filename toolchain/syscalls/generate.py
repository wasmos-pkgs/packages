#!/usr/bin/env python3
"""Generate canonical `wali.SYS_*` bindings for musl (C) and wasmos-abi (Rust).

Reads Linux asm-generic (`aarch64`) syscall numbers from musl's
`arch/aarch64/bits/syscall.h.in` and emits:
  1. `toolchain/musl-arch/wasm64/bits/wali_syscalls.h`
  2. `crates/abi/src/syscalls.rs`
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
SYSCALL_IN = ROOT / "build/toolchain/sources/musl/arch/aarch64/bits/syscall.h.in"
C_OUT = ROOT / "toolchain/musl-arch/wasm64/bits/wali_syscalls.h"
RS_OUT = ROOT / "toolchain/postlink/crates/abi/src/syscalls.rs"

TYPED_SIGNATURES: dict[str, list[str]] = {
    "getcwd": ["i64", "i64"],
    "eventfd2": ["i32", "i32"],
    "epoll_create1": ["i32"],
    "epoll_ctl": ["i32", "i32", "i32", "i64"],
    "epoll_pwait": ["i32", "i64", "i32", "i32", "i64", "i64"],
    "dup": ["i32"],
    "dup3": ["i32", "i32", "i32"],
    "fcntl": ["i32", "i32", "i64"],
    "ioctl": ["i32", "i64", "i64"],
    "flock": ["i32", "i32"],
    "mknodat": ["i32", "i64", "i32", "i64"],
    "mkdirat": ["i32", "i64", "i32"],
    "unlinkat": ["i32", "i64", "i32"],
    "symlinkat": ["i64", "i32", "i64"],
    "linkat": ["i32", "i64", "i32", "i64", "i32"],
    "renameat": ["i32", "i64", "i32", "i64"],
    "statfs": ["i64", "i64"],
    "fstatfs": ["i32", "i64"],
    "truncate": ["i64", "i64"],
    "ftruncate": ["i32", "i64"],
    "fallocate": ["i32", "i32", "i64", "i64"],
    "faccessat": ["i32", "i64", "i32", "i32"],
    "chdir": ["i64"],
    "fchdir": ["i32"],
    "chroot": ["i64"],
    "fchmod": ["i32", "i32"],
    "fchmodat": ["i32", "i64", "i32", "i32"],
    "fchownat": ["i32", "i64", "i32", "i32", "i32"],
    "fchown": ["i32", "i32", "i32"],
    "openat": ["i32", "i64", "i32", "i32"],
    "close": ["i32"],
    "pipe2": ["i64", "i32"],
    "getdents64": ["i32", "i64", "i64"],
    "lseek": ["i32", "i64", "i32"],
    "read": ["i32", "i64", "i64"],
    "write": ["i32", "i64", "i64"],
    "readv": ["i32", "i64", "i32"],
    "writev": ["i32", "i64", "i32"],
    "pread64": ["i32", "i64", "i64", "i64"],
    "pwrite64": ["i32", "i64", "i64", "i64"],
    "preadv": ["i32", "i64", "i32", "i64", "i64"],
    "pwritev": ["i32", "i64", "i32", "i64", "i64"],
    "sendfile": ["i32", "i32", "i64", "i64"],
    "pselect6": ["i32", "i64", "i64", "i64", "i64", "i64"],
    "ppoll": ["i64", "i64", "i64", "i64", "i64"],
    "signalfd4": ["i32", "i64", "i64", "i32"],
    "readlinkat": ["i32", "i64", "i64", "i64"],
    "newfstatat": ["i32", "i64", "i64", "i32"],
    "fstat": ["i32", "i64"],
    "sync": [],
    "fsync": ["i32"],
    "fdatasync": ["i32"],
    "timerfd_create": ["i32", "i32"],
    "timerfd_settime": ["i32", "i32", "i64", "i64"],
    "timerfd_gettime": ["i32", "i64"],
    "utimensat": ["i32", "i64", "i64", "i32"],
    "capget": ["i64", "i64"],
    "capset": ["i64", "i64"],
    "exit": ["i32"],
    "exit_group": ["i32"],
    "waitid": ["i32", "i32", "i64", "i32", "i64"],
    "set_tid_address": ["i64"],
    "futex": ["i64", "i32", "i32", "i64", "i64", "i32"],
    "set_robust_list": ["i64", "i64"],
    "get_robust_list": ["i32", "i64", "i64"],
    "nanosleep": ["i64", "i64"],
    "getitimer": ["i32", "i64"],
    "setitimer": ["i32", "i64", "i64"],
    "timer_create": ["i32", "i64", "i64"],
    "timer_gettime": ["i64", "i64"],
    "timer_getoverrun": ["i64"],
    "timer_settime": ["i64", "i32", "i64", "i64"],
    "timer_delete": ["i64"],
    "clock_settime": ["i32", "i64"],
    "clock_gettime": ["i32", "i64"],
    "clock_getres": ["i32", "i64"],
    "clock_nanosleep": ["i32", "i32", "i64", "i64"],
    "sched_setparam": ["i32", "i64"],
    "sched_setscheduler": ["i32", "i32", "i64"],
    "sched_getscheduler": ["i32"],
    "sched_getparam": ["i32", "i64"],
    "sched_setaffinity": ["i32", "i64", "i64"],
    "sched_getaffinity": ["i32", "i64", "i64"],
    "sched_yield": [],
    "sched_get_priority_max": ["i32"],
    "sched_get_priority_min": ["i32"],
    "sched_rr_get_interval": ["i32", "i64"],
    "kill": ["i32", "i32"],
    "tkill": ["i32", "i32"],
    "tgkill": ["i32", "i32", "i32"],
    "sigaltstack": ["i64", "i64"],
    "rt_sigsuspend": ["i64", "i64"],
    "rt_sigaction": ["i32", "i64", "i64", "i64"],
    "rt_sigprocmask": ["i32", "i64", "i64", "i64"],
    "rt_sigpending": ["i64", "i64"],
    "rt_sigtimedwait": ["i64", "i64", "i64", "i64"],
    "rt_sigqueueinfo": ["i32", "i32", "i64"],
    "rt_sigreturn": [],
    "setpriority": ["i32", "i32", "i32"],
    "getpriority": ["i32", "i32"],
    "setregid": ["i32", "i32"],
    "setgid": ["i32"],
    "setreuid": ["i32", "i32"],
    "setuid": ["i32"],
    "setresuid": ["i32", "i32", "i32"],
    "getresuid": ["i64", "i64", "i64"],
    "setresgid": ["i32", "i32", "i32"],
    "getresgid": ["i64", "i64", "i64"],
    "setfsuid": ["i32"],
    "setfsgid": ["i32"],
    "times": ["i64"],
    "setpgid": ["i32", "i32"],
    "getpgid": ["i32"],
    "getsid": ["i32"],
    "setsid": [],
    "getgroups": ["i32", "i64"],
    "setgroups": ["i32", "i64"],
    "uname": ["i64"],
    "sethostname": ["i64", "i64"],
    "setdomainname": ["i64", "i64"],
    "getrlimit": ["i32", "i64"],
    "setrlimit": ["i32", "i64"],
    "getrusage": ["i32", "i64"],
    "umask": ["i32"],
    "prctl": ["i32", "i64", "i64", "i64", "i64"],
    "getcpu": ["i64", "i64", "i64"],
    "gettimeofday": ["i64", "i64"],
    "settimeofday": ["i64", "i64"],
    "getpid": [],
    "getppid": [],
    "getuid": [],
    "geteuid": [],
    "getgid": [],
    "getegid": [],
    "gettid": [],
    "sysinfo": ["i64"],
    "socket": ["i32", "i32", "i32"],
    "socketpair": ["i32", "i32", "i32", "i64"],
    "bind": ["i32", "i64", "i32"],
    "listen": ["i32", "i32"],
    "accept": ["i32", "i64", "i64"],
    "connect": ["i32", "i64", "i32"],
    "getsockname": ["i32", "i64", "i64"],
    "getpeername": ["i32", "i64", "i64"],
    "sendto": ["i32", "i64", "i64", "i32", "i64", "i32"],
    "recvfrom": ["i32", "i64", "i64", "i32", "i64", "i64"],
    "setsockopt": ["i32", "i32", "i32", "i64", "i32"],
    "getsockopt": ["i32", "i32", "i32", "i64", "i64"],
    "shutdown": ["i32", "i32"],
    "sendmsg": ["i32", "i64", "i32"],
    "recvmsg": ["i32", "i64", "i32"],
    "brk": ["i64"],
    "munmap": ["i64", "i64"],
    "mremap": ["i64", "i64", "i64", "i32", "i64"],
    "clone": ["i64", "i64", "i64", "i64", "i64"],
    "execve": ["i64", "i64", "i64"],
    "mmap": ["i64", "i64", "i32", "i32", "i32", "i64"],
    "fadvise64": ["i32", "i64", "i64", "i32"],
    "mprotect": ["i64", "i64", "i32"],
    "msync": ["i64", "i64", "i32"],
    "mlock": ["i64", "i64"],
    "munlock": ["i64", "i64"],
    "mlockall": ["i32"],
    "munlockall": [],
    "mincore": ["i64", "i64", "i64"],
    "madvise": ["i64", "i64", "i32"],
    "rt_tgsigqueueinfo": ["i32", "i32", "i32", "i64"],
    "accept4": ["i32", "i64", "i64", "i32"],
    "recvmmsg": ["i32", "i64", "i32", "i32", "i64"],
    "wait4": ["i32", "i64", "i32", "i64"],
    "prlimit64": ["i32", "i32", "i64", "i64"],
    "sendmmsg": ["i32", "i64", "i32", "i32"],
    "renameat2": ["i32", "i64", "i32", "i64", "i32"],
    "getrandom": ["i64", "i64", "i32"],
    "memfd_create": ["i64", "i32"],
    "execveat": ["i32", "i64", "i64", "i64", "i32"],
    "membarrier": ["i32", "i32", "i32"],
    "mlock2": ["i64", "i64", "i32"],
    "copy_file_range": ["i32", "i64", "i32", "i64", "i64", "i32"],
    "preadv2": ["i32", "i64", "i32", "i64", "i64", "i32"],
    "pwritev2": ["i32", "i64", "i32", "i64", "i64", "i32"],
    "statx": ["i32", "i64", "i32", "i32", "i64"],
    "pidfd_send_signal": ["i32", "i32", "i64", "i32"],
    "pidfd_open": ["i32", "i32"],
    "clone3": ["i64", "i64"],
    "close_range": ["i32", "i32", "i32"],
    "openat2": ["i32", "i64", "i64", "i64"],
    "pidfd_getfd": ["i32", "i32", "i32"],
    "faccessat2": ["i32", "i64", "i32", "i32"],
    "epoll_pwait2": ["i32", "i64", "i32", "i64", "i64", "i64"],
    "fchmodat2": ["i32", "i64", "i32", "i32"],
}


def parse_syscalls() -> list[tuple[str, int]]:
    entries: list[tuple[str, int]] = []
    pattern = re.compile(r"^#define\s+__NR_(\w+)\s+(\d+)\s*$")
    for line in SYSCALL_IN.read_text(encoding="utf-8").splitlines():
        m = pattern.match(line)
        if m:
            entries.append((m.group(1), int(m.group(2))))
    return entries


def c_type(wasm_ty: str) -> str:
    return "int" if wasm_ty == "i32" else "long"


def rs_type(wasm_ty: str) -> str:
    return "WasmScalar::I32" if wasm_ty == "i32" else "WasmScalar::I64"


def emit_c_header(entries: list[tuple[str, int]]) -> None:
    C_OUT.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "/* Auto-generated by toolchain/syscalls/generate.py. Do not edit by hand. */",
        "#ifndef _BITS_WALI_SYSCALLS_H",
        "#define _BITS_WALI_SYSCALLS_H",
        "",
        '#define __WALI_IMPORT(name) __attribute__((__import_module__("wali"), __import_name__(name)))',
        "",
        "/* Dedicated process/thread/dlfcn imports */",
        '__WALI_IMPORT("SYS_fork") long __wali_sys_fork(void);',
        '__WALI_IMPORT("SYS_vfork") long __wali_sys_vfork(void);',
        '__WALI_IMPORT("SYS_thread_clone") long __wali_sys_thread_clone(long, long, long, long, long);',
        '__WALI_IMPORT("SYS_dlopen") long __wali_sys_dlopen(long, int, long, long, long, long);',
        '__WALI_IMPORT("SYS_dlsym") long __wali_sys_dlsym(long, long, long, long, long);',
        '__WALI_IMPORT("SYS_dlclose") long __wali_sys_dlclose(long);',
        '__WALI_IMPORT("SYS_dladdr") long __wali_sys_dladdr(long, long);',
        '__WALI_IMPORT("SYS__syscall") long __wali_sys_fallback(long, long, long, long, long, long, long);',
        "",
    ]
    arg_names = ["a", "b", "c", "d", "e", "f"]
    for name, _nr in entries:
        params = TYPED_SIGNATURES.get(name, ["i64"] * 6)
        c_params = ", ".join(c_type(p) for p in params) if params else "void"
        lines.append(f'__WALI_IMPORT("SYS_{name}") long __wali_sys_{name}({c_params});')

    lines.extend(
        [
            "",
            "static inline __attribute__((__always_inline__)) long __wali_dispatch(",
            "\tlong n, long a, long b, long c, long d, long e, long f)",
            "{",
            "\tif (!__builtin_constant_p(n)) {",
            "\t\tif (n == 220 && ((unsigned long)a & 0x00000100UL) == 0 && b == 0)",
            "\t\t\treturn __wali_sys_fork();",
            "\t\treturn __wali_sys_fallback(n, a, b, c, d, e, f);",
            "\t}",
            "\tswitch (n) {",
        ]
    )
    for name, nr in entries:
        params = TYPED_SIGNATURES.get(name, ["i64"] * 6)
        if name == "clone":
            lines.append(
                "\tcase 220: return (((unsigned long)a & 0x00000100UL) == 0 && b == 0) "
                "? __wali_sys_fork() : __wali_sys_clone(a, b, c, d, e);"
            )
            continue
        call_args = ", ".join(
            f"({c_type(p)}){arg_names[idx]}" for idx, p in enumerate(params)
        )
        lines.append(f"\tcase {nr}: return __wali_sys_{name}({call_args});")
    lines.extend(
        [
            "\tdefault: return __wali_sys_fallback(n, a, b, c, d, e, f);",
            "\t}",
            "}",
            "",
            "#endif /* _BITS_WALI_SYSCALLS_H */",
            "",
        ]
    )
    C_OUT.write_text("\n".join(lines), encoding="utf-8")


def emit_rust_module(entries: list[tuple[str, int]]) -> None:
    lines = [
        "//! Auto-generated canonical `wali.SYS_*` syscall definitions.",
        "//! Generated by `toolchain/syscalls/generate.py`.",
        "",
        "/// WebAssembly scalar value type in a `wali.SYS_*` import signature.",
        "#[derive(Clone, Copy, Debug, PartialEq, Eq)]",
        "pub enum WasmScalar {",
        "    I32,",
        "    I64,",
        "}",
        "",
        "/// Metadata for one `wali.SYS_*` import.",
        "#[derive(Clone, Copy, Debug, PartialEq, Eq)]",
        "pub struct SyscallSpec {",
        "    /// Linux `asm-generic` syscall number (`None` for synthetic helpers like `SYS_fork`).",
        "    pub nr: Option<u32>,",
        "    /// Import name inside the `\"wali\"` module (e.g. `\"SYS_read\"`).",
        "    pub import_name: &'static str,",
        "    /// Parameter types; return type is always `WasmScalar::I64`.",
        "    pub params: &'static [WasmScalar],",
        "    /// Whether calling this import can clone the process address space (`fork`).",
        "    pub is_process_fork: bool,",
        "}",
        "",
        'pub const WALI_MODULE: &str = "wali";',
        "",
        "#[must_use]",
        "pub fn lookup_by_import_name(name: &str) -> Option<&'static SyscallSpec> {",
        "    SYSCALL_SPECS.iter().find(|spec| spec.import_name == name)",
        "}",
        "",
        "pub const SYSCALL_SPECS: &[SyscallSpec] = &[",
        "    SyscallSpec {",
        "        nr: None,",
        '        import_name: "SYS_fork",',
        "        params: &[],",
        "        is_process_fork: true,",
        "    },",
        "    SyscallSpec {",
        "        nr: None,",
        '        import_name: "SYS_vfork",',
        "        params: &[],",
        "        is_process_fork: false,",
        "    },",
        "    SyscallSpec {",
        "        nr: None,",
        '        import_name: "SYS_thread_clone",',
        "        params: &[WasmScalar::I64, WasmScalar::I64, WasmScalar::I64, WasmScalar::I64, WasmScalar::I64],",
        "        is_process_fork: false,",
        "    },",
        "    SyscallSpec {",
        "        nr: None,",
        '        import_name: "SYS_dlopen",',
        "        params: &[WasmScalar::I64, WasmScalar::I32, WasmScalar::I64, WasmScalar::I64, WasmScalar::I64, WasmScalar::I64],",
        "        is_process_fork: false,",
        "    },",
        "    SyscallSpec {",
        "        nr: None,",
        '        import_name: "SYS_dlsym",',
        "        params: &[WasmScalar::I64, WasmScalar::I64, WasmScalar::I64, WasmScalar::I64, WasmScalar::I64],",
        "        is_process_fork: false,",
        "    },",
        "    SyscallSpec {",
        "        nr: None,",
        '        import_name: "SYS_dlclose",',
        "        params: &[WasmScalar::I64],",
        "        is_process_fork: false,",
        "    },",
        "    SyscallSpec {",
        "        nr: None,",
        '        import_name: "SYS_dladdr",',
        "        params: &[WasmScalar::I64, WasmScalar::I64],",
        "        is_process_fork: false,",
        "    },",
        "    SyscallSpec {",
        "        nr: None,",
        '        import_name: "SYS__syscall",',
        "        params: &[WasmScalar::I64, WasmScalar::I64, WasmScalar::I64, WasmScalar::I64, WasmScalar::I64, WasmScalar::I64, WasmScalar::I64],",
        "        is_process_fork: false,",
        "    },",
    ]
    fork_capable = {"clone", "clone3"}
    for name, nr in entries:
        params = TYPED_SIGNATURES.get(name, ["i64"] * 6)
        rs_params = ", ".join(rs_type(p) for p in params)
        is_fork = "true" if name in fork_capable else "false"
        lines.extend(
            [
                "    SyscallSpec {",
                f"        nr: Some({nr}),",
                f'        import_name: "SYS_{name}",',
                f"        params: &[{rs_params}],",
                f"        is_process_fork: {is_fork},",
                "    },",
            ]
        )
    lines.extend(
        [
            "];",
            "",
            "#[cfg(test)]",
            "mod tests {",
            "    use super::{lookup_by_import_name, SYSCALL_SPECS};",
            "    use std::collections::BTreeSet;",
            "",
            "    #[test]",
            "    fn syscall_import_names_are_unique_and_populated() {",
            "        assert!(SYSCALL_SPECS.len() >= 300);",
            "        let mut names = BTreeSet::new();",
            "        let mut numbers = BTreeSet::new();",
            "        for spec in SYSCALL_SPECS {",
            "            assert!(names.insert(spec.import_name));",
            "            if let Some(nr) = spec.nr {",
            "                assert!(numbers.insert(nr));",
            "            }",
            "        }",
            '        let fork = lookup_by_import_name("SYS_fork");',
            "        assert!(fork.is_some_and(|s| s.is_process_fork));",
            '        let read = lookup_by_import_name("SYS_read");',
            "        assert!(read.is_some_and(|s| !s.is_process_fork));",
            "    }",
            "}",
            "",
        ]
    )
    RS_OUT.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    entries = parse_syscalls()
    emit_c_header(entries)
    emit_rust_module(entries)
    print(f"Generated {len(entries)} asm-generic syscalls (+4 WALI helpers)")


if __name__ == "__main__":
    main()
