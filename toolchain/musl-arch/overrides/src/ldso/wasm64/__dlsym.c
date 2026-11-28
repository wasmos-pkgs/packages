#include <dlfcn.h>
#include <stdint.h>
#include "dynlink.h"
#include "syscall.h"

static void *wasm_dlsym(void *restrict p, const char *restrict s, void *restrict ra)
{
	(void)ra;
	if (!s) {
		__dl_seterr("Symbol not found: (null)");
		return 0;
	}
	uintptr_t out_addr = 0;
	char err[256];
	err[0] = 0;
	long r = __wali_sys_dlsym((long)p, (long)s, (long)&out_addr, (long)err, sizeof(err));
	if (r < 0) {
		__dl_seterr("%s", err[0] ? err : "Symbol not found");
		return 0;
	}
	return (void *)out_addr;
}

weak_alias(wasm_dlsym, __dlsym);

#if _REDIR_TIME64
weak_alias(wasm_dlsym, __dlsym_redir_time64);
#endif
