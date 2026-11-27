# wasm64-submilli-linux-musl architecture build settings
CRT_OBJS = obj/crt/wasm64/crt1.o obj/crt/crti.o obj/crt/crtn.o

# Wasm SjLj runtime support functions must not be transformed by the SjLj pass,
# while `longjmp.c` keeps `+exception-handling` to emit `__builtin_wasm_throw`.
obj/src/setjmp/wasm64/setjmp.o: CFLAGS_ALL += -mno-exception-handling
obj/src/setjmp/wasm64/longjmp.o: CFLAGS_ALL += -mllvm -wasm-enable-sjlj=false
