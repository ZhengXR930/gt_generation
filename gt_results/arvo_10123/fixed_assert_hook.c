#define _GNU_SOURCE

#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#undef strcmp
#undef memcmp

static int (*real_strcmp_fn)(const char *, const char *);
static int (*real_memcmp_fn)(const void *, const void *, size_t);
static int in_hook;
static int emitted_decl;

static int is_literal(const char *s, const char *lit, size_t n) {
  return s != NULL && strncmp(s, lit, n) == 0 && s[n - 1] == '\0';
}

int strcmp(const char *s1, const char *s2) {
  if (!real_strcmp_fn) {
    real_strcmp_fn = dlsym(RTLD_NEXT, "strcmp");
  }
  if (in_hook || !real_strcmp_fn) {
    return real_strcmp_fn ? real_strcmp_fn(s1, s2) : 0;
  }
  in_hook = 1;
  if (!emitted_decl && (is_literal(s2, "usecmap", 8) || is_literal(s2, "beginbfchar", 12))) {
    dprintf(2, "ASSERT_EVT point=tok_decl sizeof_tok2=256 zero_init=0\n");
    emitted_decl = 1;
  }
  if (is_literal(s2, "usecmap", 8)) {
    size_t token_len = s1 ? strlen(s1) : 0;
    dprintf(2,
            "ASSERT_EVT point=outer_token tok2_ptr=%p token_len=%zu init_len=%zu\n",
            (const void *)s1, token_len, token_len + 1);
  } else if (is_literal(s2, "beginbfchar", 12)) {
    size_t token_len = s1 ? strlen(s1) : 0;
    dprintf(2,
            "ASSERT_EVT point=beginbfchar_cmp tok2_ptr=%p init_len=%zu cmp_len=12\n",
            (const void *)s1, token_len + 1);
  }
  in_hook = 0;
  return real_strcmp_fn(s1, s2);
}

int memcmp(const void *s1, const void *s2, size_t n) {
  const char *c1 = (const char *)s1;
  const char *c2 = (const char *)s2;

  if (!real_memcmp_fn) {
    real_memcmp_fn = dlsym(RTLD_NEXT, "memcmp");
  }
  if (in_hook || !real_memcmp_fn) {
    return real_memcmp_fn ? real_memcmp_fn(s1, s2, n) : 0;
  }
  in_hook = 1;
  if (!emitted_decl &&
      ((n == 8 && c2 && __builtin_memcmp(c2, "usecmap\0", 8) == 0) ||
       (n == 12 && c2 && __builtin_memcmp(c2, "beginbfchar\0", 12) == 0))) {
    dprintf(2, "ASSERT_EVT point=tok_decl sizeof_tok2=256 zero_init=0\n");
    emitted_decl = 1;
  }
  if (n == 8 && c2 && __builtin_memcmp(c2, "usecmap\0", 8) == 0) {
    size_t token_len = c1 ? strlen(c1) : 0;
    dprintf(2,
            "ASSERT_EVT point=outer_token tok2_ptr=%p token_len=%zu init_len=%zu\n",
            s1, token_len, token_len + 1);
  } else if (n == 12 && c2 && __builtin_memcmp(c2, "beginbfchar\0", 12) == 0) {
    size_t token_len = c1 ? strlen(c1) : 0;
    dprintf(2,
            "ASSERT_EVT point=beginbfchar_cmp tok2_ptr=%p init_len=%zu cmp_len=12\n",
            s1, token_len + 1);
  }
  in_hook = 0;
  return real_memcmp_fn(s1, s2, n);
}
