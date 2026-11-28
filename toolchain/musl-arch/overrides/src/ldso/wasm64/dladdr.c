#define _GNU_SOURCE
#include <dlfcn.h>
#include "syscall.h"

static int wasm_dladdr(const void *addr, Dl_info *info)
{
	if (!info)
		return 0;
	long r = __wali_sys_dladdr((long)addr, (long)info);
	return r > 0 ? 1 : 0;
}

weak_alias(wasm_dladdr, dladdr);
