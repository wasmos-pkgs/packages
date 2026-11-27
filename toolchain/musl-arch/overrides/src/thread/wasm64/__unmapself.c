#include "pthread_impl.h"
#include "syscall.h"

void __unmapself(void *base, size_t size)
{
	__wali_sys_munmap((long)base, (long)size);
	for (;;)
		__wali_sys_exit(0);
}
