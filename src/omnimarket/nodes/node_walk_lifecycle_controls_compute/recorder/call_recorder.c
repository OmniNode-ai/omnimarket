// SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
// SPDX-License-Identifier: MIT
//
// Call recorder for the clean-VM walk's Mac legs (OMN-23204, plan M0 and M14).
//
// Loaded with DYLD_INSERT_LIBRARIES into every process of the graded tree. It interposes the
// network and process-creation calls and writes ONE JSON line to the file named by
// ONEX_CALL_RECORDER_LOG before each call proceeds, so an attempt is recorded whether or not it
// succeeds. Every line carries the execution identity (pid, pidversion) read at write time from
// the process's own audit token, and the Mach absolute time with the timebase that read it.
//
// Line kinds ("t"): "load" (one per executable image, from the constructor), "call" (one per
// interposed call) and "child" (written in the forked child as fork returns, by write(2) alone).
//
// Build: clang -Wno-deprecated-declarations -dynamiclib -lbsm -o call_recorder.dylib call_recorder.c
#include <arpa/inet.h>
#include <bsm/libbsm.h>
#include <dns_sd.h>
#include <fcntl.h>
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <netdb.h>
#include <netinet/in.h>
#include <spawn.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <mach-o/dyld.h>

#define INTERPOSE(replacement, replacee)                                                          \
    __attribute__((used)) static struct {                                                         \
        const void *replacement_fn;                                                               \
        const void *replacee_fn;                                                                  \
    } _interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {                    \
        (const void *)(unsigned long)&replacement, (const void *)(unsigned long)&replacee};

static int pidversion_now(void) {
    audit_token_t token;
    mach_msg_type_number_t count = TASK_AUDIT_TOKEN_COUNT;
    if (task_info(mach_task_self(), TASK_AUDIT_TOKEN, (task_info_t)&token, &count) != KERN_SUCCESS) {
        return -1;
    }
    return (int)audit_token_to_pidversion(token);
}

static void json_escape(const char *in, char *out, size_t cap) {
    size_t o = 0;
    for (size_t i = 0; in && in[i] && o + 7 < cap; i++) {
        unsigned char c = (unsigned char)in[i];
        if (c == '"' || c == '\\') {
            out[o++] = '\\';
            out[o++] = (char)c;
        } else if (c < 0x20 || c > 0x7e) {
            o += (size_t)snprintf(out + o, cap - o, "\\u%04x", c);
        } else {
            out[o++] = (char)c;
        }
    }
    out[o] = '\0';
}

// One line per write(2) on an O_APPEND descriptor, so concurrent processes do not interleave.
static void emit(const char *kind, const char *call, const char *detail, int ppid_override) {
    const char *path = getenv("ONEX_CALL_RECORDER_LOG");
    if (!path || !*path) return;
    int fd = open(path, O_WRONLY | O_APPEND | O_CREAT, 0600);
    if (fd < 0) return;
    mach_timebase_info_data_t tb;
    mach_timebase_info(&tb);
    char esc_detail[1024], esc_image[1024], image[1024] = "";
    uint32_t size = sizeof(image);
    if (_NSGetExecutablePath(image, &size) != 0) {
        image[0] = '\0';
    } else {
        // The observer names an exec target by its resolved path, so the load line does too.
        char resolved[1024];
        if (realpath(image, resolved)) snprintf(image, sizeof(image), "%s", resolved);
    }
    json_escape(detail ? detail : "", esc_detail, sizeof(esc_detail));
    json_escape(image, esc_image, sizeof(esc_image));
    char line[3072];
    int n = snprintf(line, sizeof(line),
        "{\"t\":\"%s\",\"call\":\"%s\",\"pid\":%d,\"pidversion\":%d,\"ppid\":%d,\"image\":\"%s\","
        "\"mach_abs\":%llu,\"tb_numer\":%u,\"tb_denom\":%u,\"detail\":\"%s\"}\n",
        kind, call, (int)getpid(), pidversion_now(), ppid_override >= 0 ? ppid_override : (int)getppid(),
        esc_image, (unsigned long long)mach_absolute_time(), tb.numer, tb.denom, esc_detail);
    if (n > 0) (void)!write(fd, line, (size_t)(n < (int)sizeof(line) ? n : (int)sizeof(line) - 1));
    close(fd);
}

static void describe_sockaddr(const struct sockaddr *sa, char *out, size_t cap) {
    char host[INET6_ADDRSTRLEN] = "";
    if (!sa) { snprintf(out, cap, "null"); return; }
    if (sa->sa_family == AF_INET) {
        const struct sockaddr_in *in = (const struct sockaddr_in *)sa;
        inet_ntop(AF_INET, &in->sin_addr, host, sizeof(host));
        snprintf(out, cap, "inet %s:%d", host, ntohs(in->sin_port));
    } else if (sa->sa_family == AF_INET6) {
        const struct sockaddr_in6 *in6 = (const struct sockaddr_in6 *)sa;
        inet_ntop(AF_INET6, &in6->sin6_addr, host, sizeof(host));
        snprintf(out, cap, "inet6 %s:%d", host, ntohs(in6->sin6_port));
    } else if (sa->sa_family == AF_UNIX) {
        snprintf(out, cap, "unix %s", ((const struct sockaddr_un *)sa)->sun_path);
    } else {
        snprintf(out, cap, "family %d", sa->sa_family);
    }
}

__attribute__((constructor)) static void recorder_load(void) { emit("load", "load", "", -1); }

static int rec_connect(int fd, const struct sockaddr *sa, socklen_t len) {
    char d[256];
    describe_sockaddr(sa, d, sizeof(d));
    emit("call", "connect", d, -1);
    return connect(fd, sa, len);
}
INTERPOSE(rec_connect, connect)

static int rec_connectx(int fd, const sa_endpoints_t *ep, sae_associd_t aid, unsigned int flags,
                        const struct iovec *iov, unsigned int iovcnt, size_t *len, sae_connid_t *cid) {
    char d[256];
    describe_sockaddr(ep ? ep->sae_dstaddr : NULL, d, sizeof(d));
    emit("call", "connectx", d, -1);
    return connectx(fd, ep, aid, flags, iov, iovcnt, len, cid);
}
INTERPOSE(rec_connectx, connectx)

static int rec_getaddrinfo(const char *node, const char *service, const struct addrinfo *hints,
                           struct addrinfo **res) {
    emit("call", "getaddrinfo", node ? node : "", -1);
    return getaddrinfo(node, service, hints, res);
}
INTERPOSE(rec_getaddrinfo, getaddrinfo)

static struct hostent *rec_gethostbyname(const char *name) {
    emit("call", "gethostbyname", name ? name : "", -1);
    return gethostbyname(name);
}
INTERPOSE(rec_gethostbyname, gethostbyname)

static struct hostent *rec_gethostbyname2(const char *name, int af) {
    emit("call", "gethostbyname2", name ? name : "", -1);
    return gethostbyname2(name, af);
}
INTERPOSE(rec_gethostbyname2, gethostbyname2)

static DNSServiceErrorType rec_DNSServiceGetAddrInfo(DNSServiceRef *ref, DNSServiceFlags flags,
        uint32_t iface, DNSServiceProtocol proto, const char *host, DNSServiceGetAddrInfoReply cb, void *ctx) {
    emit("call", "DNSServiceGetAddrInfo", host ? host : "", -1);
    return DNSServiceGetAddrInfo(ref, flags, iface, proto, host, cb, ctx);
}
INTERPOSE(rec_DNSServiceGetAddrInfo, DNSServiceGetAddrInfo)

static DNSServiceErrorType rec_DNSServiceQueryRecord(DNSServiceRef *ref, DNSServiceFlags flags,
        uint32_t iface, const char *name, uint16_t rrtype, uint16_t rrclass, DNSServiceQueryRecordReply cb, void *ctx) {
    emit("call", "DNSServiceQueryRecord", name ? name : "", -1);
    return DNSServiceQueryRecord(ref, flags, iface, name, rrtype, rrclass, cb, ctx);
}
INTERPOSE(rec_DNSServiceQueryRecord, DNSServiceQueryRecord)

static int rec_posix_spawn(pid_t *pid, const char *path, const posix_spawn_file_actions_t *fa,
                           const posix_spawnattr_t *attr, char *const argv[], char *const envp[]) {
    emit("call", "posix_spawn", path ? path : "", -1);
    return posix_spawn(pid, path, fa, attr, argv, envp);
}
INTERPOSE(rec_posix_spawn, posix_spawn)

static int rec_posix_spawnp(pid_t *pid, const char *file, const posix_spawn_file_actions_t *fa,
                            const posix_spawnattr_t *attr, char *const argv[], char *const envp[]) {
    emit("call", "posix_spawnp", file ? file : "", -1);
    return posix_spawnp(pid, file, fa, attr, argv, envp);
}
INTERPOSE(rec_posix_spawnp, posix_spawnp)

static int rec_execve(const char *path, char *const argv[], char *const envp[]) {
    emit("call", "execve", path ? path : "", -1);
    return execve(path, argv, envp);
}
INTERPOSE(rec_execve, execve)

static pid_t rec_fork(void) {
    emit("call", "fork", "", -1);
    int parent_pid = (int)getpid();
    pid_t child = fork();
    if (child == 0) emit("child", "fork", "", parent_pid);
    return child;
}
INTERPOSE(rec_fork, fork)

// vfork's child shares the parent's stack and cannot return through a wrapper, so it is served by
// fork: a conforming vfork caller only execs or exits in the child, which fork supports.
static pid_t rec_vfork(void) {
    emit("call", "vfork", "", -1);
    int parent_pid = (int)getpid();
    pid_t child = fork();
    if (child == 0) emit("child", "vfork", "", parent_pid);
    return child;
}
INTERPOSE(rec_vfork, vfork)
