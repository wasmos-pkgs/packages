#define _GNU_SOURCE
#include <dlfcn.h>
#include <stddef.h>
#include <stdint.h>
#include "dynlink.h"
#include "libc.h"
#include "syscall.h"

#define DL_MAX_CTORS 512

weak void _init(void);
extern weak hidden void (*const __init_array_start)(void), (*const __init_array_end)(void);

static void run_wasm_ctors(const uintptr_t *ctors)
{
	size_t n = (size_t)ctors[0];
	if (n >= DL_MAX_CTORS)
		n = DL_MAX_CTORS - 1;
	for (size_t i = 1; i <= n; i++) {
		void (*fn)(void) = (void (*)(void))ctors[i];
		if (fn)
			fn();
	}
}

void __libc_start_init(void)
{
	uintptr_t ctors[DL_MAX_CTORS];
	char err[64];
	ctors[0] = 0;
	if (__wali_sys_dlopen(0, RTLD_LAZY | RTLD_GLOBAL, (long)ctors, DL_MAX_CTORS, (long)err, sizeof(err)) > 0
	    && ctors[0] > 0) {
		run_wasm_ctors(ctors);
		return;
	}
	if (_init)
		_init();
	uintptr_t a = (uintptr_t)&__init_array_start;
	for (; a < (uintptr_t)&__init_array_end; a += sizeof(void (*)(void)))
		(*(void (**)(void))a)();
}

static void *wasm_dlopen(const char *file, int mode)
{
	uintptr_t ctors[DL_MAX_CTORS];
	char err[256];
	ctors[0] = 0;
	err[0] = 0;
	long h = __wali_sys_dlopen((long)file, mode, (long)ctors, DL_MAX_CTORS, (long)err, sizeof(err));
	if (h <= 0) {
		__dl_seterr("%s", err[0] ? err : "Dynamic loading failed");
		return 0;
	}
	if (ctors[0] > 0)
		run_wasm_ctors(ctors);
	return (void *)(uintptr_t)h;
}

weak_alias(wasm_dlopen, dlopen);

void _dlstart(void)
{
	static const char msg[] = "musl libc (wasm64) 1.2.5\n";
	__wali_sys_write(2, (long)msg, sizeof(msg) - 1);
	for (;;)
		__wali_sys_exit_group(0);
}
