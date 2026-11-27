#define fp_barrierf fp_barrierf
static inline float fp_barrierf(float x)
{
	volatile float y = x;
	return y;
}

#define fp_barrier fp_barrier
static inline double fp_barrier(double x)
{
	volatile double y = x;
	return y;
}

#define fp_force_evalf fp_force_evalf
static inline void fp_force_evalf(float x)
{
	volatile float y;
	y = x;
	(void)y;
}

#define fp_force_eval fp_force_eval
static inline void fp_force_eval(double x)
{
	volatile double y;
	y = x;
	(void)y;
}
