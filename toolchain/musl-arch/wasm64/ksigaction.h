#include <features.h>

/* Matches Linux 64-bit asm-generic `struct sigaction` layout (with SA_RESTORER). */
struct k_sigaction {
	void (*handler)(int);
	unsigned long flags;
#ifdef SA_RESTORER
	void (*restorer)(void);
#endif
	unsigned long mask[_NSIG / 8 / sizeof(long)];
};

hidden void __restore(void), __restore_rt(void);
