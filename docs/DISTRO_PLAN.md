# Distro plan — from `autokernel world` to a host-optimized, hardened Linux

Status: plan, 2026-10-13. Supersedes the v0.17–v1.0 arc in
[ROADMAP.md](ROADMAP.md) and extends [WORLD.md](WORLD.md) (whose W0–W8
milestones are re-sequenced here, not discarded).

**Goal.** A Debian/Ubuntu-derived distribution whose packages are rebuilt
from source with flags, features and package selection chosen for a
specific host class and goal (security or performance). The choices are
made by a solver that an LLM feeds proposals to, and every choice is
validated by a real build, test and benchmark before it ships. Security
updates are rebuilt automatically, fast enough that running it is safer
than stock.

- **v1** is an OCI image: a drop-in for `ubuntu:26.04`, rebuilt with
  clang, ThinLTO, an ISA tier and a hardening floor. It is signed, ships
  an SBOM, and is rebuilt automatically after every security update.
- **v2** is a bootable, image-based host: the laptop and the homelab
  servers, using the autokernel kernel.
- **v3+** is the configure engine. It picks the packages, their build
  features and their flags per host, with the LLM proposing and the
  solver, build and benchmarks deciding.

---

## 1. Where we are (audit, 2026-10-13)

**Already proven in June, on ring 0 only (51 source packages):**
- 49/51 built; 44 of those genuinely with clang (38 also with ThinLTO).
- $2.36 of LLM spend and 7.2 CPU-hours.
- An agent patched apt's clang C++ incompatibility.
- The assembled image boots under KVM with the clang-built systemd as
  PID 1.
- VM boot time went from 1.6 s to ~240 ms.
- PGO made sqlite3 12–20% faster.

**What exists in code:**
- `world init/plan/build/decide/image/boot-test/adopt` in
  `src/autokernel/world/`.
- The build path is sbuild in unshare mode, then a flat signed repo, then
  mmdebstrap and an ext4 image.
- FTBFS triage uses a fixed list of remedies, merges overrides, and calls
  a patching agent when needed.

**Not built:**
- W4 (autopkgtest, piuparts, auto-revert).
- W6 (security watcher).
- W7 (ring 1/2 at scale).
- W8 (closed loop).
- OCI output.
- The remote builder.

**Lost or at risk:**
- None of the June state is on this laptop or under `/nas4`: the
  exceptions tables, `patches/apt.patch`, the PGO profiles and the
  sbuild chroots.
- The narrative survives in `docs/experiment/`, but the knowledge base
  has to be recovered or rebuilt.

**Defects to fix before building more:**
1. **Version suffix never changes.** It is always `+ak1`, so a flags or
   PGO rebuild publishes different contents under the same version and
   adopted hosts never upgrade.
2. **Flag audit is likely vacuous.** It matches a token anywhere in the
   build log, so the echoed build environment satisfies it.
3. **Hardening tiers are mislabelled.** `PARANOID` produces the same
   flags as `FORTIFY_PLUS`, and there is no RELRO, BIND_NOW, CET or
   auto-var-init anywhere.
4. **Retry rebuild drops existing overrides,** and the agentic tier is
   passed a pydantic-ai model ID instead of a CLI model name.
5. **Publishing is O(N²).** `publish` re-indexes everything after each
   build, and every build gets every `.deb` as `--extra-package`. That
   won't survive ring 2's ~1,700 sources.
6. **`-march=native` is the default,** which is wrong for anything
   distributed.
7. **Global `nocheck`** at the aggressive tier: the clang world has
   never run a package test suite.

**Fleet available:**

| Host | CPU | ISA | Role |
|---|---|---|---|
| s0 | i9-12900K, 24 threads, 123 GB | v3 | primary builder, repo |
| s5, s6, s7 | Ryzen 7840HS, 16 threads, 26 GB | **v4 (Zen 4)** | builders, v4 test targets |
| s4 | i5-12600K, 2× 4060 Ti | v3 | ML evals; not a builder |
| s1, s2 | Xeon E3, 4 cores | v3 | test targets only |
| laptop | Core Ultra 5 125H (Meteor Lake) | **v3, no AVX-512** | daily driver; v2 target |

The fleet mixes ISA levels, so per-host `-march=native` builds can't be
shared. Every artifact is keyed by an ISA tier instead (§3.1).

---

## 2. What the research says (and what it changes)

Full notes: four research reports, 2026-10-13. Sources inline.

1. **Wolfi/Chainguard is the model for v1, and the CVE pipeline is
   what they actually sell.**
   - Their stack is declarative recipes (melange), images assembled
     without a shell (apko), and SLSA provenance, Sigstore signatures
     and an SBOM on every artifact.
   - Their SLA is critical fixes in 7 days and KEV-listed CVEs in 1 day.
   - They replaced an event-driven pipeline that "became a cascading
     mess" with a **reconciliation loop**: the desired state is "latest
     upstream, zero known CVEs", and bots, some AI-driven, work a queue
     until reality matches it.
   - We copy that architecture (§3.4).
2. **Flags alone are worth ~1%.**
   - Canonical's archive-wide amd64v3 rebuild found ~1% for most
     packages, and FESCo rejected Fedora's v3 change for lack of
     evidence.
   - The real gains are concentrated: PGO+BOLT gives 10–30% on hot
     binaries; `-fno-semantic-interposition` gives 5–27% on CPython;
     ThinLTO gives 2–10%.
   - ⇒ Build a **benchmark harness before claiming performance**, and
     spend the effort on the few hot packages.
3. **Switching to clang silently loses Ubuntu's hardening** (verified on
   this host).
   - Ubuntu's gcc enables `_FORTIFY_SOURCE=3`, SSP, BIND_NOW and CET
     markings in its specs, so they apply even to packages that ignore
     `dpkg-buildflags`.
   - clang 21 enables none of these by default, and `dpkg-buildflags`
     reports `bindnow=no`.
   - Separately, clang treats `-flto=auto` as *full* LTO.
   - ⇒ The flag floor must be explicit and **enforced on the output
     binaries** (checksec/annobin-style), not inferred from the log.
4. **The hardening worth having costs ~0–3%:**
   - FORTIFY=3 (~4× more coverage than level 2, no measurable SPEC cost)
   - `-fstack-protector-strong`, `-fstack-clash-protection`
   - full RELRO + BIND_NOW + PIE
   - `-ftrivial-auto-var-init=zero`
   - `_GLIBCXX_ASSERTIONS` / libc++ fast hardening (Google measured
     ~0.3%)
   - `-fcf-protection=full` with shadow stack enabled at runtime.
   
   Each of these has a known exception list in Fedora, nixpkgs, Wolfi
   and Chimera.
5. **What not to do by default:**
   - **Clang CFI:** Chimera turned it off by default; it breaks on
     mismatched callback signatures and needs hidden visibility. Make it
     opt-in for small daemons.
   - **A global `LD_PRELOAD` of hardened_malloc:** Kicksecure deprecated
     it after browser, Xorg and PHP breakage and a 6× slowdown in
     `cryptsetup`.
   - **SafeStack:** unmaintained.
   - **`-fbounds-safety`:** not in upstream clang 21.
6. **Shadow stack is off by default everywhere,** including here. It is
   enabled per process with `GLIBC_TUNABLES=glibc.cpu.hwcaps=SHSTK`, and
   one unmarked DSO disables it, or makes `dlopen` fail. That makes it a
   *closure* property, which is exactly the kind of constraint the
   solver should own.
7. **ISA tiers:**
   - Ubuntu 26.04 already ships `amd64v3` variant indices
     (`APT::Architecture-Variants`).
   - glibc-hwcaps lets one rootfs carry v3 copies of hot libraries, as
     openSUSE does.
   - RHEL's silent v3-under-`x86_64` is the anti-pattern: make the tier
     visible and **fail loudly on a mismatched CPU**.
8. **Spack's concretizer is the model for the engine:**
   - Typed variants with `when=` conditions, `conflicts()`, and
     microarchitecture targets from archspec, solved by clingo (Answer
     Set Programming).
   - Hard constraints, then soft preferences in lexicographic priority
     bands, with explainable solves and reuse of existing binaries as a
     scored criterion.
   - Exception data worth importing: nixpkgs `hardeningDisable`, Gentoo
     `filter-flags`, Yocto `security_flags` and `PACKAGECONFIG`.
9. **The real feature space is upstream,** not in Debian.
   - Only 2.6% of main sources expose a `pkg.*` build profile.
   - Options come from meson introspect (typed JSON), cargo features,
     `cmake -LAH`, and autotools `AC_ARG_*` (free text, which an LLM can
     normalize).
10. **Where LLMs help, per the evidence:**
    - **As pruners and samplers inside a measured loop.** LLM4Perf
      improved baselines in 91.5% of cases.
    - **As repair agents with preserved evidence.** EvidenT repaired
      53.9% of system-level package failures, versus 1.8% for direct
      LLM repair. Ubuntu's own agentic-FTBFS pipeline built 86% of its
      attempted fixes at ~$0.36 each.
    - **Not choosing flags blind.** That hallucinates and reasons
      superficially.
    - Most build failures are environment or dependency problems, which
      a configure engine handles well.
11. **Infrastructure:**
    - **Image builder:** mkosi (v27) has `Format=oci` *and*
      disk/UKI/sysext/sysupdate, so one config can cover v1 and v2.
    - **Repo:** aptly, for snapshots and atomic `publish switch`.
    - **Build farm:** debusine is the future fleet build+QA platform
      (sbuild/autopkgtest/piuparts/blhc tasks, regression tracking
      against a reference). But it is pre-1.0, had 2 CVEs in 2026, and
      Ubuntu support is untested. Start with DIY sbuild and keep
      debusine as a P3 evaluation.
    - **Not used:** OBS (stalled in Debian) and bootc (no
      Debian/Ubuntu support; composefs not production-ready). Revisit
      bootc in 2027.
12. **Governance:**
    - Debian's August 2026 general resolution permits responsible AI
      use but requires human accountability and prior consensus for
      large automated actions.
    - Gentoo and NetBSD ban AI contributions.
    - Our agent patches stay **ours** unless a human reviews each one
      before upstreaming.

---

## 3. Architecture

Four layers. The machinery is the same at every ring and version; only N
and the decision space grow.

```
            ┌───────────────────────── desired state ─────────────────────────┐
            │  profile (goal, threat, workload) × host class (ISA, kernel)    │
            │  × package set × per-package decisions  → lockfile              │
            └──────────────┬──────────────────────────────────▲──────────────┘
   L1 facts                │ L2 decide (solver + LLM)          │ measurements,
   archive indices ────────►  catalog + exceptions + policy ───┤ failures →
   host snapshot           │  → solved build plan              │ new facts
   option catalog          ▼                                   │
                    L3 factory (reconciler)                    │
                    queue → builders (s0,s5-7) → gates G1–G5 ──┘
                           → aptly staging snapshot → promote
                           ▼
                    L4 delivery
                    mkosi → OCI (v1) | disk/UKI + sysupdate (v2)
                    cosign sign + SBOM + SLSA provenance
```

### 3.1 Identity and versioning (fix first)

**Artifact key:**
```
(source, upstream_version, isa_tier, profile, flags_hash, toolchain_id, archive_snapshot)
```
- `toolchain_id` is the build chroot tarball's digest.
- `archive_snapshot` is a snapshot.ubuntu.com timestamp.

**Version string:**
- `<ubuntu_version>+ak<N>`, where `N` increases every time the key
  changes.
- Rebuild records map `N` to the key. Fixes defect 1.
- It sorts above stock, and below the next Ubuntu upload (which is the
  rebuild trigger; §3.4).

**ISA tiers:** `v2` (portable fallback), `v3` (default: laptop, s0, s4),
`v4` (Zen 4 servers; only for packages where G5 shows a win).
- Each tier is a separate aptly component and image tag
  (`…:26.04-v3-sec`).
- Images check `ld.so --help` at entrypoint and refuse to run on a CPU
  below their tier.
- `-march=native` survives only as a measured per-host experiment, never
  as a published artifact.

**Profiles:** `sec` (the hardening floor plus extras, cost-capped at
~3%) and `perf` (the floor minus nothing that costs ~0, plus
PGO/BOLT/`-fno-semantic-interposition` where measured). The floor is
shared, and **no profile ever ships below it** (a policy gate).

### 3.2 The flag floor (both profiles)

The full rationale and costs are in §2.4. Starting point (clang 21):

```
CPPFLAGS  -D_FORTIFY_SOURCE=3 -D_GLIBCXX_ASSERTIONS
          (libc++: -D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_FAST)
CFLAGS    -O2 -march=x86-64-v3 -flto=thin -ffat-lto-objects
          -fstack-protector-strong -fstack-clash-protection -fcf-protection=full
          -ftrivial-auto-var-init=zero
          -fno-omit-frame-pointer -mno-omit-leaf-frame-pointer   # keeps AutoFDO/perf viable
LDFLAGS   -fuse-ld=lld (bfd+LLVMgold where symver bites) -flto=thin
          -Wl,-z,relro -Wl,-z,now -Wl,-z,noexecstack -Wl,--as-needed
DEB_BUILD_MAINT_OPTIONS=hardening=+all
```

- **How the flags get in:** they're baked into the build chroot's
  `/etc/dpkg/buildflags.conf` (`STRIP -flto=auto`, `APPEND …`), plus
  `CC=clang CXX=clang++`. The flags are then part of `toolchain_id`
  rather than per-call environment, which is also how debusine
  environments work.
- **Per-package downgrades** are typed overrides with a reason and
  evidence, never free-text flags: `fortify=2`, `lto=off`,
  `autoinit=off`, `compiler=gcc`.
- **Imported exceptions:** seed the exceptions table from nixpkgs
  `hardeningDisable`, Wolfi, and the June triage results.

**Verification (G3) runs on the output ELF files, not the log:**
- PIE, BIND_NOW, RELRO, NX, a `.note.gnu.property` with IBT and SHSTK,
  and `__*_chk` symbol presence for FORTIFY.
- Compiler identity from `.comment` or the producer string.
- Use `-Wl,-z,cet-report=error` in the gate to find unmarked objects.
- This replaces the vacuous log audit (defect 2).

### 3.3 The decision engine (L2): solver first, LLM proposes

**Representation** (Spack-shaped, solved with clingo, a pip-installable
Answer Set Programming solver):

- **Host facts** (deterministic, from the existing snapshot code plus
  archspec):
  - microarchitecture, ISA tier and CPU flags
  - whether the kernel has `X86_USER_SHADOW_STACK`, and other kernel
    config facts from the autokernel snapshot
  - role and threat axes
- **Package facts:**
  - binary→source map, dependencies and build-dependencies (`closure.py`)
  - security notices (USNs) and popularity/usage from the host
- **Option catalog**, one record per upstream build option, in Yocto
  `PACKAGECONFIG` shape:
  ```
  {pkg, option, type, values, default, debian_current,
   arg_on, arg_off, build_deps_delta, runtime_deps_delta, conflicts,
   security_relevance, perf_relevance, provenance}
  ```
  Extracted statically:
  - meson introspect, cargo metadata, `cmake -LAH`, `AC_ARG_*` parsing
  - the `debian/rules` configure line, for the current choice
  - The LLM normalizes autotools free text and labels relevance.
- **Constraints** (hard):
  - ISA tier ≤ the minimum host in the class.
  - SHSTK on ⇒ the whole closure is CET-marked and contains no JIT or
    BOLT-rewritten objects.
  - `-fno-plt` ⇒ BIND_NOW.
  - CFI ⇒ ThinLTO + hidden visibility.
  - Load-bearing packages can't be dropped.
  - Every imported or learned exception becomes a `conflicts()` fact.
- **Objective** (lexicographic, as in Spack):
  1. policy violations
  2. explicit requirements
  3. the goal score (security or performance)
  4. rebuild cost
  5. reuse of an already-built or stock binary
- **Explanations:** `world solve --explain` prints which criterion
  forced each decision.

**LLM roles.** The LLM always proposes and never decides; each role's
output is a fact or a candidate that the gates validate:

| Role | Output | Validated by |
|---|---|---|
| catalog normalizer | typed option records from autotools/CMake text | G1 build with the option toggled |
| surface pruner | "drop `--with-ldap` on a laptop", "these 3–10 options matter for perf" | G1–G4, then G5 for perf claims |
| FTBFS/test repair (exists: `triage.py`, `agent_patch.py`) | typed remedy or patch, evidence preserved | rebuild + G2–G4 |
| exception miner | candidate `conflicts()` facts from failure clusters | reproduction on a second package or version |
| workload author | PGO training and benchmark scripts per hot package | G5 noise check (≥5 runs, CI above ~2%) |
| manifest proposer (`world decide`, now wired in) | package keep/drop for a profile | the image boots and the workload smoke test passes |

**Validation ladder** (cheapest first, every result cached by artifact
key):

| Gate | Check |
|---|---|
| G0 | solve is satisfiable |
| G1 | sbuild succeeds |
| G2 | `dh_auto_test` + autopkgtest (podman; qemu for systemd/kernel-adjacent packages) |
| G3 | ELF hardening and compiler-identity verification; abidiff against stock for libraries |
| G4 | piuparts (install/upgrade from stock), image boot, role smoke test, SHSTK tunable on |
| G5 | benchmarks, only for perf claims |

**Learning:**
- Failures become `conflicts()` facts that carry across hosts and
  versions.
- G5 data fits a sparse performance-influence model per hot package,
  with the LLM's relevance labels as the prior.
- Re-measure only when the source version or the toolchain major
  version changes.

### 3.4 The factory (L3): a reconciler, not a pipeline

- **Desired state:** for each (profile, tier), every source in the
  manifest is at the archive-latest version, built with the solved plan,
  and has passed its gates.
- **Observed state:** build records plus the aptly snapshots.
- **Reconcile loop** (hourly, on s0):
  1. **Diff** an aptly mirror of `resolute{,-updates,-security}` `Sources`
     against the build records. Any newer upstream version, changed
     key, or new exception means the item is enqueued.
  2. **Prioritize:** security-pocket uploads first, using Ubuntu's OSV
     feed for CVE and urgency labels. Watch the archive pockets, not the
     advisories, because advisories lag and many fixes land without a
     USN.
  3. **Dispatch** to builders: SSH to s0 and s5–s7 running
     `sbuild --chroot-mode=unshare` against a pinned chroot tarball,
     results returned via rsync. The queue is SQLite on s0 (Postgres
     only if needed).
  4. **Gates G1–G4,** then publish to an aptly **staging** snapshot,
     rebuild the images, run the image gates, then
     `aptly publish switch` to promote.
  5. **On failure:** triage, then the repair agent, bounded. If it still
     fails, **publish stock for that package** (`use_stock`, with honesty
     debt recorded) rather than hold back a security fix.
- **SLA target:** with stock as the fallback, a security-pocket upload
  is in a promoted snapshot within 24 h. Track this in `world status`.
- **Fixes on the way:** per-build `--extra-package` is replaced by a
  build-time apt source pointing at the staging snapshot (defect 5), and
  aptly indexes incrementally.

### 3.5 Delivery (L4)

- **v1 OCI:** mkosi `Format=oci` from a pinned aptly snapshot.
  - Container profile: no systemd or udev, no VM fstab, autologin or
    sentinel.
  - Built reproducibly (`SourceDateEpoch`).
  - `os-release` keeps `ID_LIKE=ubuntu` and `VERSION_ID=26.04` so
    grype/trivy match advisories correctly. We also publish a small
    OSV/VEX feed for `+ak` versions.
  - Signed with `cosign sign` + `cosign attest` (SPDX SBOM from syft or
    debsbom, plus SLSA provenance carrying the chroot digest, the
    buildflags digest and the `.dsc` digests).
  - Pushed to a registry on s0 (zot or distribution).
- **v2 bootable:** the same mkosi tree with `Format=disk`:
  - systemd-repart, a read-only `/usr` with dm-verity, A/B `/usr`
    partitions, and signed UKIs (the autokernel kernel published into
    the same repo).
  - Updates via `systemd-sysupdate`; ParticleOS is the reference.
  - An `/etc` overlay keeps machine state (a stateless `/etc` in the
    Clear Linux style).

---

## 4. Phases

Each phase has a live-validated exit criterion (house rule). Durations
are rough calendar estimates, not hours.

### P0 — recover and repair (≈1 week)

1. **Recover the June state** from the build host: `exceptions.json`
   files, `patches/`, PGO profiles, `reports/`. Move them into the repo
   under `world/knowledge/` (exceptions plus patches with provenance,
   keyed by version). Only the bulky outputs go on the NAS.
2. **Fix defects 1–6:** the `+akN` version key, the ELF-based G3 audit,
   the real hardening floor, override merging on retry, the agent model
   name, and ISA tiers replacing `native`.
3. **Pin the archive** with snapshot.ubuntu.com timestamps, and put the
   chroot digest into `toolchain_id`.
4. **Split the world verbs** out of `cli.py` (4,188 lines) into
   `cli_world.py`. Fix the `patches` commands that are defined after
   `__main__`.
5. **Install the toolchain** on s0 (sbuild, mmdebstrap, aptly, blhc,
   autopkgtest, piuparts, mkosi 27 from upstream, cosign, syft, grype).
6. **Build a benchmark harness for hot packages**, extended from
   `scripts/vmtest`:
   - xz, zstd, sqlite, python3 (pyperformance subset), openssl speed,
     gzip, grep, git.
   - Stock vs rebuilt, pinned CPU, ≥5 runs, Mann-Whitney test.
   - It reuses the bench-kernel approach, including the AC-power guard.

*Exit:* ring 0 rebuilds on s0 at v3 with the floor. G3 fails a
deliberately unhardened package and passes the rest. Rebuilding with
different flags produces `+ak2` and `apt upgrade` picks it up in a
chroot. The harness prints stock-vs-rebuilt for the 8 benchmarks.

### P1 — v1.0: hardened `ubuntu:26.04` replacement image (≈3–4 weeks)

- **Package set:** the `ubuntu:26.04` OCI image's package set (about 90
  binary packages), plus `ca-certificates`, `curl` and `python3-minimal`
  as a useful superset. Tier v3, profile `sec`.
- **Gates:** G1–G4 on every package (autopkgtest is back; global
  `nocheck` is removed). Packages that can't pass are `use_stock` and
  listed.
- **aptly:** mirror, local `+ak` repo, staging/promoted snapshots, an
  ed25519 key, and a keyring `.deb`.
- **Image:** mkosi OCI, cosign signature, SBOM, provenance, and the
  ISA-tier entrypoint guard.
- **Report card**, published with each release:
  - size vs `ubuntu:26.04`
  - % of ELF files that are clang / ThinLTO / FORTIFY / BIND_NOW /
    CET-marked, vs stock
  - grype findings vs stock
  - the benchmark deltas
  - the `use_stock` list (honesty debt)

*Exit:* `docker run ak/ubuntu:26.04-v3-sec` works as a base for a
Dockerfile that builds and tests a real project (autokernel's own test
suite). `cosign verify` passes. The report card shows ≥95% of ELF files
meeting the floor, with every exception explained.

### P2 — v1.1: the factory (≈3 weeks)

- The reconciler from §3.4 on s0, with builders on s0 and s5–s7 and
  SQLite queue/state.
- `world status`: lag per package vs the archive, SLA, honesty debt.
- Notifications on failures and on SLA breaches.
- A `v2` tier build and a `v4` experiment on s5 (G5 decides whether v4
  ships for any package).

*Exit:* replay a real Ubuntu security upload (using a snapshot.ubuntu.com
pair): detect, rebuild, gate, promote and rebuild the image inside one
tick, under 24 h. A deliberately broken rebuild is caught by G2/G4 and
falls back to stock automatically, with a notification.

### P3 — v1.2: profiles and the hot path (≈4 weeks)

- **More images:** `python` (3.x with PGO and
  `-fno-semantic-interposition`), `build` (gcc/clang/cmake; needs a
  toolchain-gate decision), and `postgres` (PGO). Ring 1 (~150 sources)
  through the factory.
- **PGO/BOLT/AutoFDO** as package metadata. Clear Linux's
  `profile_payload` idea: training commands live with the package's
  override, and profiles are versioned artifacts keyed by flags_hash.
- **glibc-hwcaps** v3/v4 copies of hot libraries (zstd, zlib-ng, libxml2,
  openssl) in the portable v2 image.
- **Run the debusine evaluation in parallel:** self-host it on s0 with
  one worker and one Ubuntu environment carrying our buildflags. Decide
  whether to adopt it for P5's scale or keep the DIY dispatcher.

*Exit:* the python image is ≥10% faster than stock on pyperformance (or
we publish that it isn't), with gates green and SLA met for 2 weeks
unattended.

### P4 — v2.0: bootable hosts (≈6–8 weeks)

1. **Server first:** mkosi disk image for s1/s2 (expendable, v3). UKI
   with the autokernel kernel, A/B `/usr` with dm-verity, sysupdate from
   s0. Ring 1 plus server role packages.
2. **Shadow stack rollout:** the solver computes the SHSTK-safe closure.
   Enable it via tunables per systemd unit, then system-wide on servers.
   Measure breakage.
3. **Laptop:**
   - **Ring 2:** this host's ~1,700 sources. Monster packages (llvm,
     rustc, firefox/chromium) are `use_stock` at first. Multi-day
     resumable builds.
   - **Adoption:** `world adopt` refuses to run unless the factory has
     met its SLA for 30 days.
   - **Rollback:** `world unadopt` pins stock back in one command.
   - **Order:** the laptop moves to the image-based layout last; until
     then it uses the apt-pinned repo on the existing install.

*Exit:* s1 runs the image for 30 days with ≥2 automatic A/B updates and
one forced rollback exercised. The laptop runs the ring-2 repo for 2
weeks with no regressions in the daily-driver checklist (browser,
ghostty, power profiles, suspend, printing, Docker).

### P5 — v3: the configure engine (open-ended; staged)

1. **Option catalog** for ring 1, then ring 2: static extractors plus LLM
   normalization. Target ~5–15k options across main, each with
   provenance.
2. **clingo solver** over host facts, the catalog, exceptions and
   policy. `world solve --explain`. At first it re-derives today's flags
   and overrides exactly (a regression test against the hand-built
   plan).
3. **Feature trimming for attack surface** (profile `sec`): the LLM
   proposes options to drop, the solver checks the dependency deltas,
   and G1–G4 validate. Measure attack surface with a concrete proxy:
   exported symbols, linked libraries, listening sockets, setuid
   binaries, binary size.
4. **Perf search** (profile `perf`): the LLM prunes each hot package to
   3–10 options, then a factorial or adaptive sample is measured through
   G5, and a sparse performance-influence model is fitted per package.
5. **Closed loop with the kernel:** `iterate` fitness spans the kernel
   and the world. Couple the constraints (SHSTK ⇄
   `X86_USER_SHADOW_STACK`; hardened_malloc ⇄ `vm.max_map_count`; Landlock
   available ⇄ the sandboxing features xz/systemd use). Rounds may
   propose kernel trims *and* manifest or option changes, with
   auto-revert on red gates.
6. **Pass 2 / toolchain:** rebuild glibc, gcc and llvm under the
   engine, behind an explicit opt-in.

*Exit (v3.0):* for a new host class (e.g. a "homelab Docker host"),
`world propose → solve → build → image` produces a bootable,
gate-green image without hand edits. The solve explanation accounts for
every non-default option, and the report card shows a measurable
attack-surface reduction vs P4's image at ≤3% performance cost.

---

## 5. Metrics (the report card, every release)

**Security:**
- % of ELF files meeting the floor.
- % CET-marked, and the SHSTK-enabled processes at runtime.
- Count of `use_stock` packages and active downgrades, each with a
  reason.
- grype/trivy findings vs stock.
- Security-update lag: median and p95 hours from archive to promoted.

**Performance:** the hot-package benchmark deltas with confidence
intervals; boot time; image size.

**Cost:** CPU-hours (real CPU time, not wall clock; fixes an economics
bug) and LLM dollars per rebuild, per repair, per release.

**Honesty:** any claim without a G5 measurement is labelled
"unmeasured".

## 6. Risks

| Risk | Mitigation |
|---|---|
| Rebuilt system lags security fixes | Reconciler + SLA tracking + stock fallback; adoption gated on 30 days of met SLA |
| Clang long tail (FTBFS, silent miscompiles like June's systemd one) | Every package goes through G2–G4; typed per-package downgrades; gcc fallback is honest debt, not failure |
| Hardening silently missing | G3 checks the ELF output, not the logs |
| ISA mismatch crashes | Tier in the version, image tag and aptly component; entrypoint guard; v2 fallback image |
| Performance claims are noise | Harness first (P0); G5 rules; publish null results |
| Maintenance treadmill (this is a one-person distro) | Reconciler automation; stock fallback; keep the package set small until P4; measure hours per week spent |
| LLM cost or behaviour drift | Content-addressed caches; pinned model IDs per role; LLM output is never trusted without a gate |
| Tooling maturity (debusine pre-1.0, mkosi moving fast, bootc not ready) | DIY path stays primary until P3's evaluation; pin versions |
| Laptop breakage (cf. the userns incident) | The laptop adopts last; one-command unadopt; daily-driver checklist as a gate |

## 7. Decisions for Mike

1. **Base:** Ubuntu 26.04 (this host, amd64v3 machinery, USN/OSV feeds)
   vs Debian forky (reproducible builds, upstream dpkg). *Recommended:*
   Ubuntu 26.04 now; keep `DistroSpec` portable.
2. **v1 default profile:** `sec` (recommended; the floor costs ~0–3%) or
   `perf`.
3. **Registry/repo host:** s0 (recommended) or the NAS.
4. **Where the June experiment ran** (the host with `/nas4` and
   `/data1/autokernel`), so P0 can recover it.
5. **Publishing images beyond the homelab** (e.g. GHCR)? This affects
   signing (keyless Sigstore vs a local key).

---

## 8. Handoff — starting P0 on the build server

This plan was written on the laptop (mobile5), which has no world state.
Work continues on a server with `/nas4` mounted. First session checklist:

1. **Find the June experiment state.** The docs reference
   `/nas4/data/autokernel/world/{ring0-gcc,ring0-clang,ring0-clang2,profiles,patches,reports}`
   and ccache/kbuild under `/data1/autokernel` (see `docs/experiment/DIARY.md`).
   `ls /nas4/data/autokernel/world` came back empty from the laptop. Check
   whether the NAS export path differs, and look for `exceptions.json`,
   `patches/apt.patch` and `*.profdata`. If it's gone, the knowledge base
   is rebuilt from the DIARY (the remedies are all written down there).
2. Copy the recovered exceptions and patches into the repo
   (`world/knowledge/`, §4 P0.1) **before** changing any code, so the
   P0 fixes can be regression-tested against the June results.
3. `uv sync`. Run the world unit tests (`uv run pytest tests -k world`).
   Install the toolchain from §4 P0.5.
4. Fix the defects in §1, in this order: version key (1), ELF audit (2),
   flag floor (3) together with the ISA tier (6), retry overrides and the
   agent model (4). Each fix gets a test.
5. Rebuild ring 0 at `v3`/`sec` and compare with the June ring0-clang2
   (49/51) to get the P0 exit.

Fleet facts: s0 = i9-12900K/24 threads/123 GB (primary builder); s5–s7 =
Zen 4 7840HS, 16 threads (v4); s4 runs ML evals (not a builder); s1/s2
are small (test targets); s3 was unreachable on 2026-10-09.
