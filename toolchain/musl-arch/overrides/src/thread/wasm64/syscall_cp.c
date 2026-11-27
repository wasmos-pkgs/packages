#include "pthread_impl.h"
#include "syscall.h"

static long dummy_cancel(void)
{
	return 0;
}
weak_alias(dummy_cancel, __cancel);

hidden const char __cp_begin[1] = { 0 };
hidden const char __cp_end[1] = { 0 };
hidden const char __cp_cancel[1] = { 0 };

hidden void __wasm_cp_check(void)
{
	pthread_t self = __pthread_self();
	if (self && self->cancel && (!self->canceldisable || self->cancelasync))
		__cancel();
}

hidden long __wasm_cp_finish(long r)
{
	if (r == -EINTR) {
		pthread_t self = __pthread_self();
		if (self && self->cancel && !self->canceldisable)
			return __cancel();
	}
	return r;
}

hidden long __syscall_cp_asm(volatile void *cancel_ptr, syscall_arg_t nr,
                             syscall_arg_t u, syscall_arg_t v, syscall_arg_t w,
                             syscall_arg_t x, syscall_arg_t y, syscall_arg_t z)
{
	if (*(volatile int *)cancel_ptr)
		return __cancel();
	return __wali_sys_fallback(nr, u, v, w, x, y, z);
}
