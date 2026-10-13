# Carried kernel patches

autokernel builds vanilla kernels. When a host needs a change that isn't
upstream yet, carry it as a **patch set** instead of hand-patching a source
tree. Patch sets are versioned in this repo, applied by `autokernel build
--patches`, and recorded in each build's `build.json`.

## Layout

```
patches/<set>/meta.toml     # what it is, which kernels, per-patch upstream status, required Kconfig
patches/<set>/series        # application order (quilt-compatible)
patches/<set>/NNNN-*.patch  # `git format-patch` output (authorship + Signed-off-by kept)
```

`meta.toml`:

```toml
description = "..."
kind = "upstream-pending"   # upstream-pending | out-of-tree | backport
min_version = "7.3-rc1"     # inclusive, optional
max_version = "7.4"         # exclusive, optional (rc sorts before release)

[config]                    # optional: Kconfig this set requires;
SECURITY_APPARMOR_RESTRICT_USERNS = "n"   # set before olddefconfig, verified after

[patches."0001-foo.patch"]
status = "posted"           # posted | accepted | merged | out-of-tree
link = "https://lore.kernel.org/..."
upstream_subject = "..."
```

Override the root with `$AUTOKERNEL_PATCHES_DIR` or `--patches-dir`.

## Use

```bash
autokernel patches list
autokernel patches check landlock-counted-by ubuntu-apparmor-userns \
    --kernel-source ~/.cache/autokernel/kernels/linux-torvalds-v7.3-rc6-ak
autokernel build SNAPSHOT --kernel-source TREE --compiler llvm --lto thin \
    --patches landlock-counted-by,ubuntu-apparmor-userns --execute
```

- The source tree must be a **git work tree with no modified tracked files**
  (untracked build output is fine). Tarball / `apt-get source` trees are
  rejected; fetch with git (e.g. a shallow clone + `git worktree add`).
- Patches are applied with `git am --3way` onto a new branch
  `autokernel/<UTC timestamp>` at the tree's current HEAD. The original
  commit is untouched; on conflict the tree is restored and the branch deleted.
- A patch whose reverse already applies is reported `present` and skipped,
  so a tree (or a newer upstream release) that already contains the change
  doesn't fail the build. When a set is entirely `present` on a new kernel,
  it has been merged: mark it `merged` and stop passing it.
- `check` dry-runs in a throwaway worktree; it never touches the tree.
- Sets declare a kernel range; `build` refuses sets outside it (`check`
  only warns), so a stale backport is noticed instead of silently applied.

## Provenance

`<snapshot>/build/<ts>/build.json` records the kernel version, source HEAD,
base commit, branch, compiler, LTO, the Kconfig the sets required, and per
patch: set, file, subject, sha256, result (`applied` / `present`), upstream
status and link. `kernel_release` and the built packages are added after
`make` succeeds.

## Current sets

| set | kind | why |
|---|---|---|
| `landlock-counted-by` | backport (merged) | clang miscomputes `__counted_by` for a FAM inside a union in `landlock_domain`; FORTIFY panics on every nested `landlock_restrict_self()` (xz 5.8 triggers it). Only for 7.3-rc1..rc6: v7.3-rc7 fixed it upstream by dropping the annotation ("landlock: Drop __counted_by from landlock_domain.handled_masks"). LLVM fix is llvm-project#228309. |
| `ubuntu-apparmor-userns` | out-of-tree | Ubuntu SAUCE (apparmor5.0.0 [9-13,17,18,49/57], John Johansen / Ryan Lee) adding `kernel.apparmor_restrict_unprivileged_userns`. Vanilla AppArmor only mediates userns for *confined* tasks, so on an autokernel build any unprivileged process gets full capabilities in a new user namespace (nf_tables etc. reachable). With the set + Ubuntu's `/usr/lib/sysctl.d/10-apparmor.conf`, unconfined tasks are moved to the `unprivileged_userns` profile (no capabilities) while profiled apps (`chrome`, `code`, `slack`, ...) keep `userns`. Requires `SECURITY_APPARMOR_RESTRICT_USERNS=n` like Ubuntu (default-on denies userns before apparmor.service loads the profile, LP#1990064). Verify with `scripts/verify-userns-restrict.sh`. |

Do **not** use `user.max_user_namespaces=0` as a substitute: it breaks
Chrome's sandbox and every systemd unit using `PrivateUsers=`
(power-profiles-daemon, upower, colord, uuidd).

## Refreshing for a new kernel

1. `autokernel patches check <set> --kernel-source <new tree>`.
2. All `present` → upstream merged it: set `status = "merged"`, stop using it.
3. Conflict → `git am -3` the series by hand in a scratch worktree, fix,
   `git format-patch` back into the set (note the port in the commit body),
   bump `min_version`/`max_version`.
