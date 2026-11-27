#include <features.h>
#include <signal.h>
#include "syscall.h"

hidden void __restore(void)
{
	__wali_sys_rt_sigreturn();
}

hidden void __restore_rt(void)
{
	__wali_sys_rt_sigreturn();
}

/* Host signal delivery entry point exported to the OS kernel. */
__attribute__((__visibility__("default")))
void __submilli_signal_deliver(void (*handler)(int, siginfo_t *, void *),
                               int sig, siginfo_t *info, void *ucontext)
{
	handler(sig, info, ucontext);
	__wali_sys_rt_sigreturn();
}
