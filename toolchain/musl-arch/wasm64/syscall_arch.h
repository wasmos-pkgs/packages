#define __SYSCALL_LL_E(x) (x)
#define __SYSCALL_LL_O(x) (x)

#include <bits/wali_syscalls.h>

static inline __attribute__((__always_inline__)) long __syscall0(long n)
{
	return __wali_dispatch(n, 0, 0, 0, 0, 0, 0);
}

static inline __attribute__((__always_inline__)) long __syscall1(long n, long a)
{
	return __wali_dispatch(n, a, 0, 0, 0, 0, 0);
}

static inline __attribute__((__always_inline__)) long __syscall2(long n, long a, long b)
{
	return __wali_dispatch(n, a, b, 0, 0, 0, 0);
}

static inline __attribute__((__always_inline__)) long __syscall3(long n, long a, long b, long c)
{
	return __wali_dispatch(n, a, b, c, 0, 0, 0);
}

static inline __attribute__((__always_inline__)) long __syscall4(long n, long a, long b, long c, long d)
{
	return __wali_dispatch(n, a, b, c, d, 0, 0);
}

static inline __attribute__((__always_inline__)) long __syscall5(long n, long a, long b, long c, long d, long e)
{
	return __wali_dispatch(n, a, b, c, d, e, 0);
}

static inline __attribute__((__always_inline__)) long __syscall6(long n, long a, long b, long c, long d, long e, long f)
{
	return __wali_dispatch(n, a, b, c, d, e, f);
}

#define IPC_64 0
