#include <unistd.h>
#include "libc.h"

weak int __main_void(void);
weak int main(int, char **, char **);

int __main_argc_argv(int argc, char **argv)
{
	if (__main_void)
		return __main_void();
	if (main)
		return main(argc, argv, __environ);
	__builtin_trap();
}
