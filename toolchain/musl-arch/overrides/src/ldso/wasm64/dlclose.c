#include <dlfcn.h>
#include "dynlink.h"
#include "syscall.h"

static int wasm_invalid_handle(void *h)
{
	if (__wali_sys_dlclose((long)h) < 0) {
		__dl_seterr("Invalid library handle %p", h);
		return 1;
	}
	return 0;
}

weak_alias(wasm_invalid_handle, __dl_invalid_handle);

int dlclose(void *p)
{
	return __dl_invalid_handle(p);
}
