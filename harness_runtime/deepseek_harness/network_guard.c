#define _GNU_SOURCE

#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

typedef int (*connect_fn)(int, const struct sockaddr *, socklen_t);
typedef ssize_t (*sendto_fn)(
    int,
    const void *,
    size_t,
    int,
    const struct sockaddr *,
    socklen_t);

static connect_fn real_connect_fn = NULL;
static sendto_fn real_sendto_fn = NULL;

static void init_real(void) {
  if (real_connect_fn == NULL) {
    real_connect_fn = (connect_fn)dlsym(RTLD_NEXT, "connect");
  }
  if (real_sendto_fn == NULL) {
    real_sendto_fn = (sendto_fn)dlsym(RTLD_NEXT, "sendto");
  }
}

static void guard_log(const char *fmt, ...) {
  const char *quiet = getenv("POCGEN_NETWORK_GUARD_QUIET");
  if (quiet != NULL && quiet[0] != '\0') {
    return;
  }
  va_list ap;
  va_start(ap, fmt);
  dprintf(STDERR_FILENO, "[network_guard] ");
  vdprintf(STDERR_FILENO, fmt, ap);
  dprintf(STDERR_FILENO, "\n");
  va_end(ap);
}

static int token_matches(const char *list, const char *needle) {
  if (list == NULL || needle == NULL || needle[0] == '\0') {
    return 0;
  }
  size_t needle_len = strlen(needle);
  const char *cursor = list;
  while (*cursor != '\0') {
    while (*cursor == ' ' || *cursor == '\t' || *cursor == ',') {
      cursor++;
    }
    const char *end = cursor;
    while (*end != '\0' && *end != ',') {
      end++;
    }
    const char *trimmed_end = end;
    while (trimmed_end > cursor &&
           (trimmed_end[-1] == ' ' || trimmed_end[-1] == '\t')) {
      trimmed_end--;
    }
    if ((size_t)(trimmed_end - cursor) == needle_len &&
        strncmp(cursor, needle, needle_len) == 0) {
      return 1;
    }
    cursor = end;
  }
  return 0;
}

static int ipv4_allowed(uint32_t addr_network_order) {
  uint32_t addr = ntohl(addr_network_order);
  if ((addr & 0xff000000U) == 0x7f000000U) {
    return 1; // 127.0.0.0/8
  }
  char text[INET_ADDRSTRLEN];
  struct in_addr in;
  in.s_addr = addr_network_order;
  if (inet_ntop(AF_INET, &in, text, sizeof(text)) == NULL) {
    return 0;
  }
  return token_matches(getenv("POCGEN_NETWORK_GUARD_ALLOW"), text);
}

static int ipv6_allowed(const struct in6_addr *addr) {
  static const unsigned char loopback[16] = {
      0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1};
  if (memcmp(addr->s6_addr, loopback, sizeof(loopback)) == 0) {
    return 1;
  }
  char text[INET6_ADDRSTRLEN];
  if (inet_ntop(AF_INET6, addr, text, sizeof(text)) == NULL) {
    return 0;
  }
  return token_matches(getenv("POCGEN_NETWORK_GUARD_ALLOW"), text);
}

static int unix_allowed(const struct sockaddr_un *addr, socklen_t len) {
  if (len <= offsetof(struct sockaddr_un, sun_path)) {
    return 1;
  }
  size_t max_len = len - offsetof(struct sockaddr_un, sun_path);
  if (max_len == 0 || addr->sun_path[0] == '\0') {
    return 1; // abstract or unnamed sockets; keep local IPC working.
  }
  char path[sizeof(addr->sun_path) + 1];
  size_t copy_len = strnlen(addr->sun_path, max_len);
  if (copy_len > sizeof(addr->sun_path)) {
    copy_len = sizeof(addr->sun_path);
  }
  memcpy(path, addr->sun_path, copy_len);
  path[copy_len] = '\0';
  if (strstr(path, "docker.sock") != NULL ||
      strstr(path, "containerd.sock") != NULL) {
    guard_log("blocked container runtime socket %s", path);
    return 0;
  }
  return 1;
}

static int sockaddr_allowed(const struct sockaddr *addr, socklen_t len) {
  if (addr == NULL) {
    return 1;
  }
  if (addr->sa_family == AF_INET && len >= sizeof(struct sockaddr_in)) {
    const struct sockaddr_in *in = (const struct sockaddr_in *)addr;
    if (ipv4_allowed(in->sin_addr.s_addr)) {
      return 1;
    }
    char text[INET_ADDRSTRLEN];
    inet_ntop(AF_INET, &in->sin_addr, text, sizeof(text));
    guard_log("blocked external IPv4 connect/sendto to %s:%u",
              text,
              (unsigned)ntohs(in->sin_port));
    return 0;
  }
  if (addr->sa_family == AF_INET6 && len >= sizeof(struct sockaddr_in6)) {
    const struct sockaddr_in6 *in6 = (const struct sockaddr_in6 *)addr;
    if (ipv6_allowed(&in6->sin6_addr)) {
      return 1;
    }
    char text[INET6_ADDRSTRLEN];
    inet_ntop(AF_INET6, &in6->sin6_addr, text, sizeof(text));
    guard_log("blocked external IPv6 connect/sendto to [%s]:%u",
              text,
              (unsigned)ntohs(in6->sin6_port));
    return 0;
  }
  if (addr->sa_family == AF_UNIX && len >= sizeof(sa_family_t)) {
    return unix_allowed((const struct sockaddr_un *)addr, len);
  }
  return 1;
}

int connect(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
  init_real();
  if (!sockaddr_allowed(addr, addrlen)) {
    errno = EACCES;
    return -1;
  }
  return real_connect_fn(sockfd, addr, addrlen);
}

ssize_t sendto(int sockfd,
               const void *buf,
               size_t len,
               int flags,
               const struct sockaddr *dest_addr,
               socklen_t addrlen) {
  init_real();
  if (!sockaddr_allowed(dest_addr, addrlen)) {
    errno = EACCES;
    return -1;
  }
  return real_sendto_fn(sockfd, buf, len, flags, dest_addr, addrlen);
}
