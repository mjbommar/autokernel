/* enum_bench — replicate systemd unit-file enumeration syscall pattern and
 * compare a serial drop-in statx storm vs an io_uring-batched one.
 *
 * Phase A (name-map walk, ALWAYS serial — getdents/readlinkat are not io_uring ops):
 *   for each of the N search dirs: opendir + getdents loop; for each symlink with a
 *   valid unit name, readlinkat it. Collect the set of unit names seen.
 * Phase B (drop-in existence storm, the io_uring-batchable part):
 *   for each unit name x each search dir, statx "<dir>/<unit>.d" and "<dir>/<unit>".
 *   Overwhelmingly ENOENT — exactly systemd #26950's 42k-lookup pattern.
 *
 * Usage: enum_bench <searchpath_root> <serial|iouring> [queue_depth] [iters]
 * Prints syscall counts and per-phase wall time (median of iters).
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dirent.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include <errno.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <linux/stat.h>
#include <liburing.h>

#define MAX_DIRS 16
#define MAX_UNITS 4096
#define MAX_PROBES (MAX_UNITS * MAX_DIRS * 2)

static char *search_dirs[MAX_DIRS];
static int n_dirs;
static char *unit_names[MAX_UNITS];
static int n_units;

/* counters */
static long c_opendir, c_getdents_entries, c_readlinkat, c_statx_serial, c_statx_uring;
static long statx_hits, statx_miss;

static double now_ms(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000.0 + t.tv_nsec / 1e6;
}

/* crude "valid unit name": has a known unit suffix and no slash */
static int is_unit_name(const char *n) {
    if (strchr(n, '/')) return 0;
    const char *suf[] = {".service",".socket",".target",".mount",".automount",
        ".swap",".timer",".path",".slice",".scope",".device",NULL};
    size_t l = strlen(n);
    for (int i = 0; suf[i]; i++) { size_t s = strlen(suf[i]);
        if (l > s && !strcmp(n + l - s, suf[i])) return 1; }
    return 0;
}

static void add_unit(const char *n) {
    if (n_units >= MAX_UNITS) return;
    for (int i = 0; i < n_units; i++) if (!strcmp(unit_names[i], n)) return; /* dedup */
    unit_names[n_units++] = strdup(n);
}

/* Phase A: serial name-map walk (faithful to unit_file_build_name_map's syscalls) */
static double walk_serial(void) {
    double t0 = now_ms();
    char buf[4096];
    for (int i = 0; i < n_dirs; i++) {
        int dfd = open(search_dirs[i], O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        if (dfd < 0) continue;
        c_opendir++;
        for (;;) {
            long nread = syscall(SYS_getdents64, dfd, buf, sizeof buf);
            if (nread <= 0) break;
            for (long bpos = 0; bpos < nread;) {
                struct dirent64 { unsigned long long d_ino, d_off; unsigned short d_reclen;
                    unsigned char d_type; char d_name[]; } *de = (void*)(buf + bpos);
                bpos += de->d_reclen;
                if (!strcmp(de->d_name, ".") || !strcmp(de->d_name, "..")) continue;
                c_getdents_entries++;
                if (de->d_type == DT_REG && is_unit_name(de->d_name))
                    add_unit(de->d_name);
                else if (de->d_type == DT_LNK && is_unit_name(de->d_name)) {
                    char tgt[1024];
                    ssize_t r = readlinkat(dfd, de->d_name, tgt, sizeof tgt - 1);
                    c_readlinkat++;
                    if (r > 0) { tgt[r] = 0; add_unit(de->d_name); }
                }
            }
        }
        close(dfd);
    }
    return now_ms() - t0;
}

/* build the probe list: <dir>/<unit>.d and <dir>/<unit> for every (unit,dir).
 * cold!=0 appends a unique token so paths are never cached (simulates cold
 * first-boot metadata lookups that the negative-dentry cache hasn't seen). */
static char **probes; static int n_probes; static int cold_gen;
static void build_probes(int cold) {
    for (int i = 0; i < n_probes; i++) free(probes[i]);
    n_probes = 0;
    if (!probes) probes = calloc(MAX_PROBES, sizeof(char*));
    /* token must be globally unique per process+iteration so paths are never
     * pre-probed by a prior run (else NFS serves them from the negative cache
     * and the "cold" measurement is silently warm). */
    char tok[48] = ""; if (cold) snprintf(tok, sizeof tok, "-c%d-%d", (int)getpid(), cold_gen++);
    for (int u = 0; u < n_units; u++)
        for (int d = 0; d < n_dirs; d++) {
            char p[2048];
            snprintf(p, sizeof p, "%s/%s.d%s", search_dirs[d], unit_names[u], tok);
            probes[n_probes++] = strdup(p);
            snprintf(p, sizeof p, "%s/%s%s", search_dirs[d], unit_names[u], tok);
            probes[n_probes++] = strdup(p);
        }
}

/* the in-memory existing-path set systemd builds (unit_path_cache) */
static char **existing; static int n_existing;
static int cmp_s(const void *a, const void *b){ return strcmp(*(char**)a,*(char**)b); }
static void collect_existing(void) {
    /* every real entry in the search dirs = what unit_file_build_name_map's path_cache holds */
    n_existing = 0; existing = calloc(MAX_PROBES, sizeof(char*));
    char buf[4096];
    for (int i = 0; i < n_dirs; i++) {
        int dfd = open(search_dirs[i], O_RDONLY|O_DIRECTORY|O_CLOEXEC); if (dfd<0) continue;
        for(;;){ long nr=syscall(SYS_getdents64,dfd,buf,sizeof buf); if(nr<=0)break;
            for(long b=0;b<nr;){ struct {unsigned long long i,o;unsigned short rl;unsigned char t;char n[];}*de=(void*)(buf+b); b+=de->rl;
                if(de->n[0]=='.')continue; char p[2048]; snprintf(p,sizeof p,"%s/%s",search_dirs[i],de->n); existing[n_existing++]=strdup(p);}}
        close(dfd);
    }
    qsort(existing,n_existing,sizeof(char*),cmp_s);
}
/* Phase B CACHED (what systemd actually does on boot): set_contains guard, statx only hits */
static double dropin_cached(void) {
    double t0 = now_ms(); struct statx stx;
    for (int i=0;i<n_probes;i++){
        char *key=probes[i];
        if (bsearch(&key,existing,n_existing,sizeof(char*),cmp_s)) {   /* in-memory, no syscall */
            statx(AT_FDCWD,probes[i],AT_SYMLINK_NOFOLLOW,STATX_TYPE,&stx); c_statx_serial++; statx_hits++;
        } else statx_miss++;   /* skipped — NO syscall, the systemd fast path */
    }
    return now_ms()-t0;
}

/* Phase B serial: statx every probe */
static double dropin_serial(void) {
    double t0 = now_ms();
    struct statx stx;
    for (int i = 0; i < n_probes; i++) {
        int r = statx(AT_FDCWD, probes[i], AT_SYMLINK_NOFOLLOW, STATX_TYPE, &stx);
        c_statx_serial++;
        if (r == 0) statx_hits++; else statx_miss++;
    }
    return now_ms() - t0;
}

/* Phase B io_uring: batch all probe statx through one ring */
static double dropin_iouring(int qd) {
    struct io_uring ring;
    if (io_uring_queue_init(qd, &ring, 0) < 0) { perror("queue_init"); return -1; }
    struct statx *results = calloc(n_probes, sizeof(struct statx));
    double t0 = now_ms();
    int submitted = 0, completed = 0, next = 0;
    while (completed < n_probes) {
        /* fill the SQ up to qd in flight */
        while (next < n_probes && (submitted - completed) < qd) {
            struct io_uring_sqe *sqe = io_uring_get_sqe(&ring);
            if (!sqe) break;
            io_uring_prep_statx(sqe, AT_FDCWD, probes[next], AT_SYMLINK_NOFOLLOW,
                                STATX_TYPE, &results[next]);
            io_uring_sqe_set_data64(sqe, next);
            next++; submitted++;
        }
        io_uring_submit(&ring);
        /* reap whatever is ready (at least one) */
        struct io_uring_cqe *cqe;
        int r = io_uring_wait_cqe(&ring, &cqe);
        if (r < 0) break;
        unsigned head; int n = 0;
        io_uring_for_each_cqe(&ring, head, cqe) {
            if (cqe->res == 0) statx_hits++; else statx_miss++;
            c_statx_uring++; completed++; n++;
        }
        io_uring_cq_advance(&ring, n);
    }
    double dt = now_ms() - t0;
    free(results); io_uring_queue_exit(&ring);
    return dt;
}

static int cmp_d(const void *a, const void *b){ double x=*(double*)a,y=*(double*)b; return x<y?-1:x>y; }

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr,"usage: %s <root> <serial|iouring> [qd] [iters]\n",argv[0]); return 2; }
    const char *root = argv[1]; const char *mode = argv[2];
    int qd = argc > 3 ? atoi(argv[3]) : 256;
    int iters = argc > 4 ? atoi(argv[4]) : 5;
    int cold = argc > 5 ? atoi(argv[5]) : 0;

    /* search path = the 12 subdirs of root (sorted for determinism) */
    DIR *rd = opendir(root); struct dirent *de; char *names[MAX_DIRS]; int nn=0;
    while ((de = readdir(rd)) && nn < MAX_DIRS) {
        if (de->d_name[0]=='.') continue;
        names[nn++] = strdup(de->d_name);
    }
    closedir(rd);
    for (int i=0;i<nn;i++) for (int j=i+1;j<nn;j++) if (strcmp(names[i],names[j])>0){char*t=names[i];names[i]=names[j];names[j]=t;}
    for (int i=0;i<nn;i++){ char p[1024]; snprintf(p,sizeof p,"%s/%s",root,names[i]); search_dirs[n_dirs++]=strdup(p);}

    /* warm: one walk to populate unit set + probes (also warms cache) */
    walk_serial(); build_probes(0);

    double walk_ms[64], drop_ms[64];
    /* reset counters, measure */
    for (int it=0; it<iters; it++) {
        n_units=0; c_opendir=c_getdents_entries=c_readlinkat=0;
        walk_ms[it] = walk_serial();
        if (cold) build_probes(1);   /* unique uncached paths this iteration */
        c_statx_serial=c_statx_uring=statx_hits=statx_miss=0;
        if (!strcmp(mode,"serial")) drop_ms[it] = dropin_serial();
        else if (!strcmp(mode,"cached")) { collect_existing(); drop_ms[it] = dropin_cached(); }
        else drop_ms[it] = dropin_iouring(qd);
    }
    qsort(walk_ms,iters,sizeof(double),cmp_d); qsort(drop_ms,iters,sizeof(double),cmp_d);
    printf("root=%s mode=%s qd=%d iters=%d\n", root, mode, qd, iters);
    printf("  dirs=%d units=%d probes=%d\n", n_dirs, n_units, n_probes);
    printf("  PhaseA name-map walk (serial): opendir=%ld getdents_entries=%ld readlinkat=%ld | median %.2f ms\n",
           c_opendir, c_getdents_entries, c_readlinkat, walk_ms[iters/2]);
    printf("  PhaseB dropin statx storm (%s): statx=%ld hits=%ld miss=%ld | median %.2f ms\n",
           mode, (long)(c_statx_serial+c_statx_uring), statx_hits, statx_miss, drop_ms[iters/2]);
    return 0;
}
