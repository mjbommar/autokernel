// Nested landlock_restrict_self(): on clang-built 7.3-rc5 with FORTIFY_SOURCE,
// the 2nd call hits __fortify_panic in landlock_merge_ruleset()/inherit_ruleset()
// because clang loads handled_masks' __counted_by count from the wrong offset.
// Build: cc -O2 -o landlock-nested-repro landlock-nested-repro.c
#define _GNU_SOURCE
#include <linux/landlock.h>
#include <stdio.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

static int restrict_once(void)
{
	struct landlock_ruleset_attr attr = {
		.handled_access_fs = LANDLOCK_ACCESS_FS_MAKE_REG,
	};
	int fd = syscall(SYS_landlock_create_ruleset, &attr, sizeof(attr), 0);
	if (fd < 0) { perror("landlock_create_ruleset"); return -1; }
	int ret = syscall(SYS_landlock_restrict_self, fd, 0);
	if (ret) perror("landlock_restrict_self");
	close(fd);
	return ret;
}

int main(void)
{
	if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) { perror("prctl"); return 1; }
	if (restrict_once()) return 1;
	puts("layer 1 ok");
	fflush(stdout);
	if (restrict_once()) return 1;   /* nested: oopses on affected kernels */
	puts("layer 2 ok -- not affected");
	return 0;
}
