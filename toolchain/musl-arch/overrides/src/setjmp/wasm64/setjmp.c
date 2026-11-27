#include <setjmp.h>
#include <stdint.h>

struct __wasm_jmp_slot {
	void *func_invocation_id;
	int label;
};

void __wasm_setjmp(void *env, int label, void *func_invocation_id)
{
	struct __wasm_jmp_slot *slot = (struct __wasm_jmp_slot *)env;
	slot->func_invocation_id = func_invocation_id;
	slot->label = label;
}

int __wasm_setjmp_test(void *env, void *func_invocation_id)
{
	struct __wasm_jmp_slot *slot = (struct __wasm_jmp_slot *)env;
	if (slot->func_invocation_id == func_invocation_id)
		return slot->label;
	return 0;
}

#undef setjmp
#undef _setjmp

int setjmp(jmp_buf env)
{
	(void)env;
	return 0;
}

int _setjmp(jmp_buf env)
{
	(void)env;
	return 0;
}
