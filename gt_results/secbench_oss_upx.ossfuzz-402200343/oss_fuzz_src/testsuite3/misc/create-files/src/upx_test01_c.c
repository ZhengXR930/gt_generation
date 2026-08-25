/* upx_test01_c.c --

   This file is part of the UPX executable compressor.

   Copyright (C) Markus Franz Xaver Johannes Oberhumer
   All Rights Reserved.

   UPX and the UCL library are free software; you can redistribute them
   and/or modify them under the terms of the GNU General Public License as
   published by the Free Software Foundation; either version 2 of
   the License, or (at your option) any later version.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License for more details.

   You should have received a copy of the GNU General Public License
   along with this program; see the file COPYING.
   If not, write to the Free Software Foundation, Inc.,
   59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.

   Markus F.X.J. Oberhumer
   <markus@oberhumer.com>
 */

#include <stdio.h>
#include <stdint.h>
#include <time.h>
#include <dlfcn.h>
#include <pthread.h>
#if defined(__cplusplus)
#  include <vector>
#endif
#undef NDEBUG
#include <assert.h>
#if defined(MUSL) && 1
#  undef assert
   /* change __LINE__ to 1 so that our checksums don't change after small changes */
#  define assert(x) ((void)((x) || (__assert_fail(#x, __FILE__, 1, __func__),0)))
#endif

#if defined(DLL)
#  define N(x)          x ## _dll
#else
#  define N(x)          x
#endif

#undef EXTERN_CONST
#if defined(__cplusplus)
#  define EXTERN_CONST  extern
#else
#  define EXTERN_CONST  /*EMPTY*/
#endif
#undef  __weak
#define __weak          __attribute__((__weak__))

#if defined(__cplusplus) && (__cplusplus+0 >= 201103L)
#  define COMPILE_TIME_ASSERT_HEADER(e)  static_assert(e, #e);
#elif defined(__STDC_VERSION__) && (__STDC_VERSION__+0 >= 201112L)
#  define COMPILE_TIME_ASSERT_HEADER(e)  _Static_assert(e, #e);
#else
#  define COMPILE_TIME_ASSERT_HEADER(e)  /*empty*/
#endif

/*************************************************************************
// constructor
**************************************************************************/

extern uint32_t N(test01_initdata);
uint32_t N(test01_initdata) = 1;

static
__attribute__((__constructor__))
void N(test01_constructor)(void)
{
    N(test01_initdata) += 1;
}

/*************************************************************************
// data:
//   static, static_const
//   extern, extern_const
//   extern_weak, extern_weak_const
//   static_thread, extern_thread
**************************************************************************/

static uint32_t N(data01_static)[] = {
#  include "data01.h"
};
static uint32_t const N(data01_static_const)[] = {
#  include "data01.h"
};
extern uint32_t N(data01_extern)[];
uint32_t N(data01_extern)[] = {
#  include "data01.h"
};
extern uint32_t const N(data01_extern_const)[];
EXTERN_CONST uint32_t const N(data01_extern_const)[] = {
#  include "data01.h"
};
extern __weak uint32_t N(data01_extern_weak)[];
__weak uint32_t N(data01_extern_weak)[] = {
#  include "data01.h"
};
extern __weak uint32_t const N(data01_extern_weak_const)[];
EXTERN_CONST __weak uint32_t const N(data01_extern_weak_const)[] = {
#  include "data01.h"
};
static __thread uint32_t N(data01_static_thread)[] = {
#  include "data01.h"
};
extern __thread uint32_t N(data01_extern_thread)[];
__thread uint32_t N(data01_extern_thread)[] = {
#  include "data01.h"
};

static uint32_t N(data02_static)[] = {
#  include "data02.h"
};
static uint32_t const N(data02_static_const)[] = {
#  include "data02.h"
};
extern uint32_t N(data02_extern)[];
uint32_t N(data02_extern)[] = {
#  include "data02.h"
};
extern uint32_t const N(data02_extern_const)[];
EXTERN_CONST uint32_t const N(data02_extern_const)[] = {
#  include "data02.h"
};
extern __weak uint32_t N(data02_extern_weak)[];
__weak uint32_t N(data02_extern_weak)[] = {
#  include "data02.h"
};
extern __weak uint32_t const N(data02_extern_weak_const)[];
EXTERN_CONST __weak uint32_t const N(data02_extern_weak_const)[] = {
#  include "data02.h"
};
static __thread uint32_t N(data02_static_thread)[] = {
#  include "data02.h"
};
extern __thread uint32_t N(data02_extern_thread)[];
__thread uint32_t N(data02_extern_thread)[] = {
#  include "data02.h"
};

static uint32_t N(data03_static)[] = {
#  include "data03.h"
};
static uint32_t const N(data03_static_const)[] = {
#  include "data03.h"
};
extern uint32_t N(data03_extern)[];
uint32_t N(data03_extern)[] = {
#  include "data03.h"
};
extern uint32_t const N(data03_extern_const)[];
EXTERN_CONST uint32_t const N(data03_extern_const)[] = {
#  include "data03.h"
};
extern __weak uint32_t N(data03_extern_weak)[];
__weak uint32_t N(data03_extern_weak)[] = {
#  include "data03.h"
};
extern __weak uint32_t const N(data03_extern_weak_const)[];
EXTERN_CONST __weak uint32_t const N(data03_extern_weak_const)[] = {
#  include "data03.h"
};
static __thread uint32_t N(data03_static_thread)[] = {
#  include "data03.h"
};
extern __thread uint32_t N(data03_extern_thread)[];
__thread uint32_t N(data03_extern_thread)[] = {
#  include "data03.h"
};

COMPILE_TIME_ASSERT_HEADER(sizeof(N(data01_static)) == sizeof(N(data02_static)))
COMPILE_TIME_ASSERT_HEADER(sizeof(N(data01_static)) == sizeof(N(data03_static)))
COMPILE_TIME_ASSERT_HEADER(sizeof(N(data01_static)) >= 16)

/*************************************************************************
//
**************************************************************************/

typedef struct {
    int argc;
    uint32_t v;
    size_t mask;
} thread_args;

__attribute__((__noinline__)) void * N(test01_thread)(void *arg);
__attribute__((__noinline__)) void * N(test01_thread)(void *arg)
{
    thread_args *p = (thread_args *) arg;
    size_t mask = sizeof(N(data01_static)) / sizeof(N(data01_static)[0]) - 1;
    size_t i = p->argc & mask;
    uint32_t v = 0;

    if (p->argc > 999999) { // NEVER
        mask = mask >> 1;
        i = (i + 1) & mask;
        if ((i % 11) != 0) N(data01_static)[i] ^= 1;
        if ((i % 13) != 0) N(data01_extern)[i] ^= 2;
        if ((i % 17) != 0) N(data01_extern_weak)[i] ^= 3;
        if ((i % 19) != 0) N(data01_static_thread)[i] ^= 4;
        if ((i % 23) != 0) N(data01_extern_thread)[i] ^= 5;
        i = (i + 1) & mask;
        if ((i % 11) != 0) N(data02_static)[i] ^= 1;
        if ((i % 13) != 0) N(data02_extern)[i] ^= 2;
        if ((i % 17) != 0) N(data02_extern_weak)[i] ^= 3;
        if ((i % 19) != 0) N(data02_static_thread)[i] ^= 4;
        if ((i % 23) != 0) N(data02_extern_thread)[i] ^= 5;
        i = (i + 1) & mask;
        if ((i % 11) != 0) N(data03_static)[i] ^= 1;
        if ((i % 13) != 0) N(data03_extern)[i] ^= 2;
        if ((i % 17) != 0) N(data03_extern_weak)[i] ^= 3;
        if ((i % 19) != 0) N(data03_static_thread)[i] ^= 4;
        if ((i % 23) != 0) N(data03_extern_thread)[i] ^= 5;
    }

    i = (i + v) & mask;
    v += N(data01_static)[i];
    i = (i + v) & mask;
    v ^= N(data01_static_const)[i];
    i = (i + v) & mask;
    v += N(data01_extern)[i];
    i = (i + v) & mask;
    v ^= N(data01_extern_const)[i];
    i = (i + v) & mask;
    v += N(data01_extern_weak)[i];
    i = (i + v) & mask;
    v ^= N(data01_extern_weak_const)[i];
    i = (i + v) & mask;
    v += N(data01_static_thread)[i];
    i = (i + v) & mask;
    v ^= N(data01_extern_thread)[i];

    i = (i + v) & mask;
    v += N(data02_static)[i];
    i = (i + v) & mask;
    v ^= N(data02_static_const)[i];
    i = (i + v) & mask;
    v += N(data02_extern)[i];
    i = (i + v) & mask;
    v ^= N(data02_extern_const)[i];
    i = (i + v) & mask;
    v += N(data02_extern_weak)[i];
    i = (i + v) & mask;
    v ^= N(data02_extern_weak_const)[i];
    i = (i + v) & mask;
    v += N(data02_static_thread)[i];
    i = (i + v) & mask;
    v ^= N(data02_extern_thread)[i];

    i = (i + v) & mask;
    v += N(data03_static)[i];
    i = (i + v) & mask;
    v ^= N(data03_static_const)[i];
    i = (i + v) & mask;
    v += N(data03_extern)[i];
    i = (i + v) & mask;
    v ^= N(data03_extern_const)[i];
    i = (i + v) & mask;
    v += N(data03_extern_weak)[i];
    i = (i + v) & mask;
    v ^= N(data03_extern_weak_const)[i];
    i = (i + v) & mask;
    v += N(data03_static_thread)[i];
    i = (i + v) & mask;
    v ^= N(data03_extern_thread)[i];

    {
    struct timespec ts;
    ts.tv_sec = 0;
    ts.tv_nsec = 1 * 1000 * 1000; /* 1 millisecond */
    nanosleep(&ts, NULL);
    }

    p->v = v | 1;
    return NULL;
}

__attribute__((__noinline__)) uint32_t N(test01)(int argc);
__attribute__((__noinline__)) uint32_t N(test01)(int argc)
{
#if defined(__cplusplus)
    std::vector<pthread_t> t(2);
    std::vector<thread_args> p(2);
#else
    pthread_t t[2];
    thread_args p[2];
#endif

#if defined(DLL)
    assert(N(test01_initdata) == 2);
#else
    assert(N(test01_initdata) == 3);
#endif

    p[0].argc = argc;
    p[0].v = 0;
    p[1].argc = argc;
    p[1].v = 0;
    pthread_create(&t[0], NULL, N(test01_thread), &p[0]);
    pthread_create(&t[1], NULL, N(test01_thread), &p[1]);
    pthread_join(t[0], NULL);
    pthread_join(t[1], NULL);
    assert((p[0].v & 1) != 0);
    assert(p[0].v == p[1].v);
    return p[0].v;
}


/*************************************************************************
// main entry point
**************************************************************************/

#if !defined(DLL) && !defined(STATIC)
# if defined(USE_DLOPEN)
    typedef uint32_t (*test01_dll_func_t)(int argc);
# else
    extern uint32_t test01_dll(int argc);
# endif
#endif

#if !defined(DLL)
int main(int argc, char *argv[])
{
    uint32_t a, b;

    assert(N(test01_initdata) == 2);
    N(test01_initdata) += 1;

    a = N(test01)(argc);
    b = a;
    printf("upx_test_01_exe    = 0x%08lx\n", (unsigned long) a);

#if !defined(DLL) && !defined(STATIC)
# if defined(USE_DLOPEN)
    {
    void *dh;
    union { void *fh; test01_dll_func_t test01_dll; } u;
#  if defined(__cplusplus)
    dh = dlopen("lib" DLL_PREFIX "_dll_x.so", RTLD_NOW);
    assert(dh != NULL);
    u.fh = dlsym(dh, "_Z10test01_dlli"); /* C++ mangled name */
#  else
    dh = dlopen("lib" DLL_PREFIX "_dll_c.so", RTLD_NOW);
    assert(dh != NULL);
    u.fh = dlsym(dh, "test01_dll");
#  endif
    assert(u.fh != NULL);
    b = u.test01_dll(argc);
    printf("upx_test_01_dlopen = 0x%08lx\n", (unsigned long) b);
    dlclose(dh);
    }
# else
    b = test01_dll(argc);
    printf("upx_test_01_dll    = 0x%08lx\n", (unsigned long) b);
# endif
    assert(b == a);
#endif

    (void) argv;
    if (a == 0 || a != b) return 1;
    return 0;
}
#endif

/* vim:set ts=4 sw=4 et: */
