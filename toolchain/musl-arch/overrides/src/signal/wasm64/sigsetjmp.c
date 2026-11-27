#include <setjmp.h>
#include <signal.h>
#include "syscall.h"
#include "pthread_impl.h"

int __sigsetjmp_save(sigjmp_buf buf, int save)
{
	buf->__fl = (unsigned long)(save != 0);
	if (save)
		__syscall(SYS_rt_sigprocmask, SIG_SETMASK, 0, buf->__ss, _NSIG / 8);
	return 0;
}

#undef sigsetjmp
int sigsetjmp(sigjmp_buf buf, int save)
{
	__sigsetjmp_save(buf, save);
	return 0;
}
