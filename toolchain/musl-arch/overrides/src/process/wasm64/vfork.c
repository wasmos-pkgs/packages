#define _GNU_SOURCE
#include <unistd.h>
#include "syscall.h"

pid_t vfork(void)
{
	/* On Wasm, returning twice from C requires stack cloning via SYS_fork. */
	return _Fork();
}
