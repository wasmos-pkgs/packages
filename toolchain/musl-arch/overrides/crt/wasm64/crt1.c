#include <features.h>
#include "libc.h"

/* Per-thread 128-byte kernel control block in TLS (exported as TLS offset global). */
__thread unsigned char __submilli_kernel_block[128]
	__attribute__((__aligned__(16), __visibility__("default")));

extern void __wasm_call_ctors(void);
int __main_argc_argv(int, char **);
weak void _fini(void);
int __libc_start_main(int (*)(int, char **, char **), int, char **,
	void (*)(void), void (*)(void), void (*)(void));

void _init(void)
{
	__wasm_call_ctors();
}

static int __submilli_call_main(int argc, char **argv, char **envp)
{
	(void)envp;
	return __main_argc_argv(argc, argv);
}

__attribute__((__noinline__)) static void _start_c(long *p)
{
	int argc = (int)p[0];
	char **argv = (void *)(p + 1);
	/* Keep __submilli_kernel_block live across linker GC. */
	__asm__ __volatile__("" : : "r"(__submilli_kernel_block) : "memory");
	__libc_start_main(__submilli_call_main, argc, argv, _init, _fini, 0);
}

static inline long *__get_initial_sp(void)
{
	long *sp;
	__asm__(".globaltype __stack_pointer, i64\n\t"
	        "global.get __stack_pointer\n\t"
	        "local.set %0"
	        : "=r"(sp));
	return sp;
}

void _start(void)
{
	_start_c(__get_initial_sp());
}
