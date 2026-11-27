#include "pthread_impl.h"

int __set_thread_area(void *p)
{
	__submilli_pthread_self_ptr = (struct __pthread *)p;
	return 0;
}
