#include <setjmp.h>

struct __wasm_longjmp_args {
	void *env;
	int val;
};

static _Thread_local struct __wasm_longjmp_args __longjmp_payload;

_Noreturn void __wasm_longjmp(void *env, int val)
{
	if (val == 0)
		val = 1;
	__longjmp_payload.env = env;
	__longjmp_payload.val = val;
	__builtin_wasm_throw(1, &__longjmp_payload);
}

#undef longjmp
#undef _longjmp

_Noreturn void longjmp(jmp_buf env, int val)
{
	__wasm_longjmp(env, val);
}

_Noreturn void _longjmp(jmp_buf env, int val)
{
	__wasm_longjmp(env, val);
}
