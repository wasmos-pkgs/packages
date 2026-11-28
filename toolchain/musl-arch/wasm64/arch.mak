# wasm64-submilli-linux-musl architecture build settings
CRT_OBJS = obj/crt/wasm64/crt1.o obj/crt/wasm64/Scrt1.o obj/crt/crti.o obj/crt/crtn.o
LDSO_OBJS =

obj/crt/wasm64/Scrt1.o: CFLAGS_ALL += -fPIC

# Ensure libc.so carries SONAME=libc.so and enforces no undefined symbols (-z defs)
lib/libc.so: LDFLAGS_ALL += -Wl,--soname,libc.so -Wl,-z,defs

# Wasm SjLj runtime support functions must not be transformed by the SjLj pass,
# while `longjmp.c` keeps `+exception-handling` to emit `__builtin_wasm_throw`.
obj/src/setjmp/wasm64/setjmp.o obj/src/setjmp/wasm64/setjmp.lo: CFLAGS_ALL += -mno-exception-handling
obj/src/setjmp/wasm64/longjmp.o obj/src/setjmp/wasm64/longjmp.lo: CFLAGS_ALL += -mllvm -wasm-enable-sjlj=false
