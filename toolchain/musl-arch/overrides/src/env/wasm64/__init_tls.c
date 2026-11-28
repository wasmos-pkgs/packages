#define SYSCALL_NO_TLS 1
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "pthread_impl.h"
#include "libc.h"
#include "atomic.h"
#include "syscall.h"

volatile int __thread_list_lock;

_Thread_local struct __pthread *__submilli_pthread_self_ptr;

static struct __pthread main_pthread;

static inline size_t __get_wasm_tls_size(void)
{
	size_t sz;
	__asm__(".globaltype __tls_size, i64, immutable\n\t"
	        "global.get __tls_size\n\t"
	        "local.set %0"
	        : "=r"(sz));
	return sz;
}

static inline size_t __get_wasm_tls_align(void)
{
	size_t al;
	__asm__(".globaltype __tls_align, i64, immutable\n\t"
	        "global.get __tls_align\n\t"
	        "local.set %0"
	        : "=r"(al));
	return al ? al : 16;
}

int __init_tp(void *p)
{
	pthread_t td = p;
	td->self = td;
	int r = __set_thread_area(TP_ADJ(p));
	if (r < 0) return -1;
	if (!r) libc.can_do_threads = 1;
	td->detach_state = DT_JOINABLE;
	td->tid = __syscall(SYS_set_tid_address, &__thread_list_lock);
	td->locale = &libc.global_locale;
	td->robust_list.head = &td->robust_list.head;
	td->sysinfo = __sysinfo;
	td->next = td->prev = td;
	return 0;
}

void *__copy_tls(unsigned char *mem)
{
	size_t align = libc.tls_align ? libc.tls_align : 16;
	uintptr_t addr = ((uintptr_t)mem + 15U) & ~(uintptr_t)15U;
	pthread_t td = (pthread_t)addr;
	memset(td, 0, sizeof(*td));
	uintptr_t wasm_tls = (addr + sizeof(*td) + (align - 1)) & ~(uintptr_t)(align - 1);
	/* Store secondary thread's Wasm TLS block address in td->dtv for clone(). */
	td->dtv = (uintptr_t *)wasm_tls;
	return td;
}

static void static_init_tls(size_t *aux)
{
	(void)aux;
	size_t wasm_sz = __get_wasm_tls_size();
	size_t wasm_al = __get_wasm_tls_align();
	if (wasm_al < 16) wasm_al = 16;
	libc.tls_align = wasm_al;
	libc.tls_size = sizeof(struct __pthread) + wasm_sz + 2 * wasm_al + 32;
	libc.tls_cnt = 1;

	/* __wasm_init_memory has already initialized main thread's __tls_base. */
	if (__init_tp(&main_pthread) < 0)
		a_crash();
}

weak_alias(static_init_tls, __init_tls);
