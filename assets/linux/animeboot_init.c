/* AnimeBootNeo animation-only PID1. No vendor userspace, shell or disk writes.
 * The bootloader must arm and verify the next-boot Windows handoff first.
 * Both GRUB transports arm and read back the one-shot state before Linux.
 */
#define _GNU_SOURCE
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/reboot.h>
#include <sys/wait.h>
#include <sys/types.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <time.h>
#include <errno.h>

static char cmdline[16384];
static int has(const char *token) {
    size_t n = strlen(token);
    const char *p = cmdline;
    while ((p = strstr(p, token))) {
        if ((p == cmdline || p[-1] == ' ') && (p[n] == 0 || p[n] == ' ' || p[n] == '\n')) return 1;
        ++p;
    }
    return 0;
}
static void stop(int reboot_ok) {
    sync();
    reboot(reboot_ok ? RB_AUTOBOOT : RB_POWER_OFF);
    /* Never exit PID1. A reboot syscall failure must not spin a boot loop. */
    for (;;) pause();
}
int main(void) {
    if (getpid() != 1) { fputs("AnimeBootNeo init must run as PID1\n", stderr); return 2; }
    mkdir("/proc",0755); mkdir("/sys",0755); mkdir("/dev",0755); mkdir("/tmp",01777);
    if (mount("proc","/proc","proc",MS_NOSUID|MS_NODEV|MS_NOEXEC,NULL) < 0 && errno != EBUSY) stop(0);
    if (mount("devtmpfs","/dev","devtmpfs",MS_NOSUID,NULL) < 0 && errno != EBUSY) stop(0);
    if (mount("sysfs","/sys","sysfs",MS_NOSUID|MS_NODEV|MS_NOEXEC,NULL) < 0 && errno != EBUSY) stop(0);
    int fd=open("/dev/console",O_RDWR);
    if (fd>=0) { dup2(fd,0); dup2(fd,1); dup2(fd,2); if(fd>2) close(fd); }
    FILE *f=fopen("/proc/cmdline","r");
    if(f) { fgets(cmdline,sizeof(cmdline),f); fclose(f); }
    /* Normal boot is quiet in both debug modes.  The player's two diagnostic
     * pages are drawn directly into its framebuffer after playback.  A serial
     * trace is explicit test instrumentation, never ordinary console output. */
    int trace = has("AnimeBootNeoTrace=1");
    int logfd = trace ? open("/dev/ttyS0",O_WRONLY|O_NOCTTY) : -1;
    if(logfd<0) logfd=open("/dev/null",O_WRONLY);
    if(logfd>=0) { dup2(logfd,1);dup2(logfd,2);if(logfd>2)close(logfd); }
    if (!has("AnimeBootNeoMode=WindowsAnimation") || !has("AnimeBootNeoHandoff=Armed")) {
        fputs("AnimeBootNeo: missing verified bootloader handoff; stopping.\n",stderr); stop(0);
    }
    int debug = has("AnimeBootNeoDebug=1");
    if(trace) fputs("AnimeBoot Neo Debug (2610.3): init ready; starting animation\n",stderr);
    pid_t child=fork();
    if(child==0) { execl("/AnimeBootNeo/player","player",(char*)NULL); _exit(127); }
    if(child<0) stop(1);
    int status=0;
    while(waitpid(child,&status,0)<0) { if(errno!=EINTR) break; }
    if(!WIFEXITED(status) || WEXITSTATUS(status)!=0) {
        fprintf(stderr,"AnimeBoot Neo Debug: animation failed (exit=%d signal=%d); continuing Windows handoff.\n",
                WIFEXITED(status)?WEXITSTATUS(status):-1,WIFSIGNALED(status)?WTERMSIG(status):0);
        if(debug) {
            fputs("AnimeBoot Neo Debug: keeping failure details visible for 8 seconds.\n",stderr);
            struct timespec remaining={8,0};
            while(nanosleep(&remaining,&remaining)<0 && errno==EINTR) {}
        }
    }
    if(debug) fputs("AnimeBoot Neo Debug: player finished; rebooting to Windows\n",stderr);
    stop(1);
    return 0;
}
