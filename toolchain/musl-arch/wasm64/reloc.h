#define LDSO_ARCH "wasm64"
#define NO_LEGACY_INITFINI
#define TPOFF_K 0
#define CRTJMP(pc, sp) __builtin_trap()
