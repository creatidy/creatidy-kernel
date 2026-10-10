/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#if !defined(__linux__) || !defined(__x86_64__) || defined(__ILP32__)
#error "UNPROVED: requires native Linux LP64 x86-64"
#endif

static int reap(pid_t pid)
{
    int status = 0;
    pid_t result;
    do {
        result = waitpid(pid, &status, 0);
    } while (result == -1 && errno == EINTR);
    return result == pid && WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

static long raw_clone(unsigned long flags, int *saved_errno)
{
    errno = 0;
    long result = syscall(SYS_clone, flags, 0UL, 0UL, 0UL, 0UL);
    if (result == 0)
        _exit(0);
    *saved_errno = errno;
    return result;
}

static void *thread_main(void *arg)
{
    return arg;
}

int main(int argc, char **argv)
{
    int expected, error, status;
    long result;
    pthread_t thread;
    void *joined = NULL;
    static char token;
    const unsigned long cases[] = {
        CLONE_NEWUSER, CLONE_NEWNS, CLONE_NEWNET, CLONE_NEWPID,
        CLONE_NEWIPC, CLONE_NEWUTS, CLONE_NEWCGROUP,
        CLONE_NEWUSER | CLONE_NEWNS,
        CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWNET | CLONE_NEWPID |
        CLONE_NEWIPC | CLONE_NEWUTS | CLONE_NEWCGROUP
    };

    if (argc != 2 || (strcmp(argv[1], "1") && strcmp(argv[1], "33")))
        return 2;
    expected = strcmp(argv[1], "1") == 0 ? EPERM : EDOM;
    alarm(10);
    result = raw_clone(SIGCHLD, &error);
    if (result <= 0 || !reap((pid_t)result))
        return 3;
    puts("RAW_CLONE_REAPED");
    if (pthread_create(&thread, NULL, thread_main, &token) != 0 ||
        pthread_join(thread, &joined) != 0 || joined != &token)
        return 4;
    puts("PTHREAD_JOINED");
    errno = 0;
    result = syscall(SYS_clone3, 0UL, 0UL);
    if (result != -1 || errno != ENOSYS)
        return 5;
    puts("CLONE3_ENOSYS");
    for (unsigned int i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        result = raw_clone(cases[i] | SIGCHLD, &error);
        if (result > 0) {
            int reaped = reap((pid_t)result);
            fprintf(stderr, "UNEXPECTED_CHILD %u reaped=%d\n", i, reaped);
            return 6;
        }
        if (result != -1 || error != expected) {
            fprintf(stderr, "DENIAL_MISMATCH %u rc=%ld errno=%d\n", i, result, error);
            return 7;
        }
        printf("DENIED %u %d\n", i, error);
    }
    do {
        result = waitpid(-1, &status, WNOHANG);
    } while (result == -1 && errno == EINTR);
    if (result != -1 || errno != ECHILD)
        return 8;
    puts("NO_CHILDREN");
    return 0;
}
