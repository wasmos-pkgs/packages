#define _GNU_SOURCE
#include <sched.h>
#include <stdarg.h>
#include <stdint.h>
#include "pthread_impl.h"
#include "syscall.h"

extern void __wasm_init_tls(void *);

struct __submilli_thread_boot {
	int (*func)(void *);
	void *arg;
	void *wasm_tls;
	struct __pthread *td;
};

/* Secondary thread entry point invoked by the host OS on a fresh Wasm Instance. */
__attribute__((__visibility__("default")))
void __submilli_thread_start(long boot_ptr)
{
	struct __submilli_thread_boot *boot = (struct __submilli_thread_boot *)boot_ptr;
	int (*func)(void *) = boot->func;
	void *arg = boot->arg;
	void *wasm_tls = boot->wasm_tls;
	struct __pthread *td = boot->td;

	if (wasm_tls)
		__wasm_init_tls(wasm_tls);
	__submilli_pthread_self_ptr = td;

	int code = func(arg);
	for (;;)
		__wali_sys_exit(code);
}

int __clone(int (*func)(void *), void *stack, int flags, void *arg, ...)
{
	if (flags & CLONE_THREAD) {
		va_list ap;
		pid_t *ptid = 0, *ctid = 0;
		void *tls = 0;

		va_start(ap, arg);
		if (flags & (CLONE_PARENT_SETTID | CLONE_PIDFD))
			ptid = va_arg(ap, pid_t *);
		else
			(void)va_arg(ap, pid_t *);
		if (flags & CLONE_SETTLS)
			tls = va_arg(ap, void *);
		else
			(void)va_arg(ap, void *);
		if (flags & (CLONE_CHILD_SETTID | CLONE_CHILD_CLEARTID))
			ctid = va_arg(ap, pid_t *);
		va_end(ap);

		uintptr_t sp = ((uintptr_t)stack - sizeof(struct __submilli_thread_boot)) & ~(uintptr_t)15U;
		struct __submilli_thread_boot *boot = (struct __submilli_thread_boot *)sp;
		struct __pthread *td = (struct __pthread *)tls;
		boot->func = func;
		boot->arg = arg;
		boot->wasm_tls = td ? (void *)td->dtv : 0;
		boot->td = td;
		return (int)__wali_sys_thread_clone(
			(long)flags, (long)sp, (long)ptid, (long)boot, (long)ctid);
	}

	/* Process-spawning clone (e.g. musl posix_spawn with CLONE_VM|CLONE_VFORK|SIGCHLD):
	 * emulate via full SYS_fork() so the child runs func(arg) in an isolated address space. */
	long ret = __wali_sys_fork();
	if (ret == 0) {
		int code = func(arg);
		for (;;)
			__wali_sys_exit(code);
	}
	return (int)ret;
}
