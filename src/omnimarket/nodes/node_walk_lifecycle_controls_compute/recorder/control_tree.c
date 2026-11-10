// SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
// SPDX-License-Identifier: MIT
//
// Control programs for the Mac legs' lifecycle controls (plan M14). One binary, one argument:
//   forked-child  : fork; the child makes the locally blocked connect (must be recorded under the
//                   child's identity and reconciled to the observer's fork event)
//   exec-sh       : exec /bin/sh -c with a connect (SIP image: the connect is never recorded)
//   reexec-blind  : re-exec itself with DYLD_INSERT_LIBRARIES removed, connect, re-exec with it restored
//   rejected      : the locally blocked connect alone
//   lookup        : a lookup of the name in argv[2]
//   graded        : the graded tree's root: posix_spawns this binary for rejected, forked-child and
//                   lookup (twice, the first warming the resolver cache), waiting for each
// Build: clang -o control_tree control_tree.c
#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <poll.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

// 192.0.2.1 is TEST-NET-1; the guest's pf rule rejects it locally, so no packet leaves.
static void rejected_connect(void) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in sa = {0};
    sa.sin_len = sizeof(sa);
    sa.sin_family = AF_INET;
    sa.sin_port = htons(443);
    inet_pton(AF_INET, "192.0.2.1", &sa.sin_addr);
    // Non-blocking with a one second bound: macOS pf drops the packet at output and does not
    // deliver the RST of `block return` to the local socket, so a blocking connect would hang.
    fcntl(s, F_SETFL, fcntl(s, F_GETFL, 0) | O_NONBLOCK);
    int rc = connect(s, (struct sockaddr *)&sa, sizeof(sa));
    if (rc != 0) {
        struct pollfd pfd = {.fd = s, .events = POLLOUT};
        poll(&pfd, 1, 1000);
    }
    printf("connect rc=%d\n", rc);
    close(s);
}

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "rejected";
    if (!strcmp(mode, "rejected")) { rejected_connect(); return 0; }
    if (!strcmp(mode, "lookup")) {
        struct addrinfo *res = NULL;
        int rc = getaddrinfo(argc > 2 ? argv[2] : "localhost", NULL, NULL, &res);
        printf("getaddrinfo rc=%d\n", rc);
        return 0;
    }
    if (!strcmp(mode, "graded")) {
        const char *host = argc > 2 ? argv[2] : "example.com";
        char *const plans[4][4] = {
            {argv[0], "rejected", NULL, NULL},
            {argv[0], "forked-child", NULL, NULL},
            {argv[0], "lookup", (char *)host, NULL},
            {argv[0], "lookup", (char *)host, NULL},
        };
        for (int i = 0; i < 4; i++) {
            pid_t c;
            if (posix_spawn(&c, argv[0], NULL, NULL, plans[i], environ) != 0) return 1;
            int st;
            waitpid(c, &st, 0);
        }
        return 0;
    }
    if (!strcmp(mode, "forked-child")) {
        pid_t c = fork();
        if (c == 0) { rejected_connect(); _exit(0); }
        int st; waitpid(c, &st, 0);
        return 0;
    }
    if (!strcmp(mode, "exec-sh")) {
        execl("/bin/sh", "sh", "-c", "/usr/bin/nc -w 1 -z 192.0.2.1 443 </dev/null; true", (char *)NULL);
        return 1;
    }
    if (!strcmp(mode, "reexec-blind")) {
        // stage 1: re-exec this binary with the injection variable removed
        const char *self = argv[0];
        if (argc == 2) {
            unsetenv("DYLD_INSERT_LIBRARIES");
            execl(self, self, "reexec-blind", "stage2", (char *)NULL);
            return 1;
        }
        if (!strcmp(argv[2], "stage2")) {
            rejected_connect();                       // blind: no injection in this execution
            setenv("DYLD_INSERT_LIBRARIES", getenv("ONEX_RECORDER_DYLIB"), 1);
            execl(self, self, "reexec-blind", "stage3", (char *)NULL);
            return 1;
        }
        return 0;                                      // stage3: instrumented again, writes a load line
    }
    return 2;
}
