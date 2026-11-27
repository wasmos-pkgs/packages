#define a_cas a_cas
static inline int a_cas(volatile int *p, int t, int s)
{
	int old = *p;
	if (old == t)
		*p = s;
	return old;
}

#define a_cas_p a_cas_p
static inline void *a_cas_p(volatile void *p, void *t, void *s)
{
	void *volatile *vp = (void *volatile *)p;
	void *old = *vp;
	if (old == t)
		*vp = s;
	return old;
}

#define a_barrier a_barrier
static inline void a_barrier(void)
{
	__asm__ __volatile__("" : : : "memory");
}

#define a_crash a_crash
static inline void a_crash(void)
{
	__builtin_trap();
}

#define a_ctz_32 a_ctz_32
static inline int a_ctz_32(uint32_t x)
{
	return __builtin_ctz(x);
}

#define a_clz_32 a_clz_32
static inline int a_clz_32(uint32_t x)
{
	return __builtin_clz(x);
}

#define a_ctz_64 a_ctz_64
static inline int a_ctz_64(uint64_t x)
{
	return __builtin_ctzll(x);
}

#define a_clz_64 a_clz_64
static inline int a_clz_64(uint64_t x)
{
	return __builtin_clzll(x);
}
