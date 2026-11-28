#include <features.h>
#include "libc.h"

weak int __main_argc_argv(int, char **);
weak int main(int, char **, char **);
weak void _fini(void);
int __libc_start_main(int (*)(int, char **, char **), int, char **,
	void (*)(void), void (*)(void), void (*)(void));

static int __submilli_call_main(int argc, char **argv, char **envp)
{
	if (__main_argc_argv)
		return __main_argc_argv(argc, argv);
	if (main)
		return main(argc, argv, envp);
	__builtin_trap();
}

__attribute__((__noinline__)) static void _start_c(long *p)
{
	int argc = (int)p[0];
	char **argv = (void *)(p + 1);
	__libc_start_main(__submilli_call_main, argc, argv, 0, _fini, 0);
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
