extern _Thread_local struct __pthread *__submilli_pthread_self_ptr;

static inline uintptr_t __get_tp(void)
{
	return (uintptr_t)__submilli_pthread_self_ptr;
}

#define MC_PC pc
