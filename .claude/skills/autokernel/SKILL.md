---
name: autokernel
description: Scan hardware, propose minimal Linux kernel configs, review and explain CONFIG_* trims. Use when the user asks to build, trim, minimize, audit, or explain a Linux kernel config; or to investigate why a particular CONFIG_ symbol is on/off; or to map hardware to required kernel modules. Ubuntu/Debian focus.
---

# autokernel

Thin orchestrator over the `autokernel` CLI in this repo. The CLI does the
work; this skill knows the verbs, the flow, and how to translate user intent
into the right invocation.

**Never edit `.config` directly. Never run `make install` or touch
`/boot`.** Those belong to a future `build`/`install` slice that has the boot-
safety probation logic. This skill is **scan → propose → explain** only.

## Verbs

| Command | Purpose |
|---|---|
| `autokernel scan [DIR]` | Run the bash collector; write a typed snapshot to `DIR/snapshot.json`. |
| `autokernel propose DIR --autonomy=LEVEL` | Resolve required modules+configs, run deterministic trims, run the LLM agent on the uncertain pile, apply policy, write `DIR/proposal.json`. Per-batch results cached under `DIR/batches/`. |
| `autokernel review DIR [--accept-recommended | --accept-low-risk | --accept-deterministic] [--reject-subsystem S]* [--reject-pattern G]* [--interactive]` | Apply ordered bulk-decision rules to `proposal.json`'s `needs_review` list. Writes `DIR/review.json` + `DIR/auto.kfrag`. With `--interactive`, opens a Textual TUI after bulk rules — single-key bindings (a/r/d to decide, j/k navigate, s/f cycle filters, w save, q quit). The TUI is for the kernel-developer persona; bulk flags work for Claude/CI. |
| `autokernel apply DIR [--kfrag P] [--out P] [--no-validate]` | Merge the kfrag into the snapshot's running `.config`, run a load-bearing-survival check, and write `DIR/final.config`. Refuses to write (exit code 4) if any load-bearing symbol that was set in the base would end up disabled in the merge — unless `--no-validate` is passed. |
| `autokernel build DIR --kernel-source PATH [--execute] [--jobs N] [--no-ccache] [--target T] [--force-dkms]` | Drop `final.config` into the source tree as `.config`; run `make olddefconfig`. With `--execute`, also run `make -j N <target>`. Default `--target=auto` picks `bindeb-pkg` (Debian/Ubuntu), `rpm-pkg` (Fedora/SUSE), or `targz-pkg` (other) per distro family. Sets reproducibility env (KBUILD_BUILD_TIMESTAMP/USER/HOST), wraps CC with ccache when available. Refuses `--execute` when DKMS modules are present unless `--force-dkms`. Per-step logs at `DIR/build/<timestamp>/`. |
| `autokernel preflight [DIR] [--for=scan\|propose\|apply\|build\|install] [--strict]` | Runs the relevant subset of system checks: distro recognized, Python version, disk/RAM/CPU, build tools (`gcc/flex/bison/...`), dev libs (`libssl-dev/openssl-devel/...`), `dmesg` readability, Secure Boot status, plus snapshot-aware checks (running config, modinfo, DKMS) when DIR is given. Returns PASS/WARN/FAIL/SKIP per check; exits non-zero on FAIL (or WARN with `--strict`). Fix hints use the local distro's package manager. |
| `autokernel fetch-source [--kernel-version X.Y.Z] [--method auto\|apt-get-source\|tarball\|...] [--out DIR] [--dry-run]` | Distro-aware kernel source acquisition. Defaults: Debian → `apt-get source linux` (no root); Fedora/Arch/other → kernel.org tarball; SUSE → `zypper install kernel-source`; Gentoo → `emerge gentoo-sources`. `--dry-run` prints the plan without executing. Idempotent: returns the existing target dir if already extracted. |

`--autonomy` is one of `explain | advise | auto-safe | auto-bold`. Default
`advise`. Higher levels auto-apply more proposals; the **load-bearing
blocklist** (root fs, active NIC, EFI, microcode, architecture fundamentals)
is enforced regardless.

`--skip-llm` cuts the LLM stage entirely. You still get the deterministic
trims (wrong CPU vendor, absent GPU, etc.). All skipped symbols land in
`not_considered`.

`--max-candidates N` caps how many uncertain symbols are sent to the LLM.
Default 600. Symbols past the cap also land in `not_considered`.

`--force-dkms` overrides the DKMS gate that refuses `auto-*` autonomy when
DKMS modules are present. Don't use it unless the user has confirmed
rebuilds will succeed.

## Typical flow

0. **Preflight (recommended)**: `autokernel preflight --for build` to surface missing
   build tools, dev libs, or low disk/RAM *before* any expensive verb runs.
   Surface the distro-specific fix hint to the user verbatim.
1. **Scan**: `autokernel scan` (or `autokernel scan /tmp/myhost`).
2. **Read** `<DIR>/snapshot.json` — confirm CPU vendor, mounted filesystems,
   active NICs match expectations. *If `dkms` is non-empty, warn the user
   that they'll need to rebuild against any new kernel.*
3. **Propose** with `--skip-llm` first to see the deterministic-only baseline.
4. **Propose** without `--skip-llm` for the full pass. If a previous run
   was interrupted, batches are cached — the rerun is cheap.
5. **Read** `<DIR>/proposal.json`. Summarize `needs_review` to the user
   GROUPED BY SUBSYSTEM (the classifier in `subsystem.py` does this) — never
   dump 600 raw rows.
6. **Review**: pick a rule combination based on user intent. If the user
   wants a "trim safe stuff", default to `--accept-recommended` plus
   `--reject-subsystem crypto --reject-subsystem security` (the user
   should opt in to trimming those). For a more cautious user, start with
   `--accept-deterministic` only and step through the rest.
7. **Output**: the kfrag at `<DIR>/auto.kfrag` is the artifact the next
   stage consumes. Show the user the kfrag header counts and offer to page
   through individual entries if asked.
8. **Apply**: `autokernel apply <DIR>` produces `<DIR>/final.config` —
   the snapshot's running `.config` with the kfrag merged in. The verb
   refuses to write the file if validation finds the merge would disable
   a load-bearing symbol that was working before. Surface validation
   failures verbatim — they identify what would brick.
9. **Build (prepare)**: `autokernel build <DIR> --kernel-source PATH`
   drops `final.config` into the source and runs `make olddefconfig`
   (fast, ~1s). Default is "prepare only" — does NOT compile.
10. **Build (execute)**: add `--execute` to also run `make -j N bindeb-pkg`
    (slow, 15–60 min). The verb refuses `--execute` when DKMS modules are
    present unless `--force-dkms` is passed. Always show the user the
    `<DIR>/build/<timestamp>/` log dir so they can inspect on failure.
11. *Future:* `autokernel install --probation` and `rollback` —
    not yet implemented.

## Kernel source acquisition

Use `autokernel fetch-source` — it's distro-aware. Default behavior:

* **Debian/Ubuntu**: `apt-get source linux` (no root needed; produces a patched-by-distro source tree).
* **Fedora/Arch/Alpine/unknown**: kernel.org tarball download.
* **openSUSE**: `zypper install kernel-source` (root needed).
* **Gentoo**: `emerge sys-kernel/gentoo-sources` (root needed).

For specific kernel versions, pass `--kernel-version X.Y.Z`. The default
is the running `uname -r`. Use `--dry-run` to preview the plan before
executing.

Build dependencies the user needs installed beforehand vary by distro
— see `autokernel preflight --for build`. The output names the exact
packages and the install command for the local family.

## Reading the proposal

`proposal.json` shape:

```json
{
  "base_config_path": "/path/to/running_config",
  "autonomy": "advise",
  "auto_applied":   [RemovalProposal, ...],
  "needs_review":   [RemovalProposal, ...],
  "annotations":    [RemovalProposal, ...],
  "blocked":        [[RemovalProposal, "load-bearing reason"], ...],
  "not_considered": ["CONFIG_FOO", ...]
}
```

Buckets are mutually exclusive. Specifically:

* `auto_applied` — will be applied by build (not yet implemented).
* `needs_review` — proposed change requires explicit approval.
* `annotations` — explanation-only; only populated at `explain` autonomy.
* `blocked` — would-be proposal vetoed by the load-bearing policy.
* `not_considered` — candidate trims that never reached the LLM (truncated
  or skipped). Surface this if the user asks "did you consider X?" — the
  answer may be "no, it was deferred."

`RemovalProposal` fields:
- `config` — the symbol, e.g. `CONFIG_INTEL_IDLE`
- `current_value` / `proposed_value` — `y`, `m`, `n`
- `reason` — human-readable rationale
- `risk` — `low` / `medium` / `high`
- `confidence` — 0..1
- `source` — `deterministic` (hardcoded rule), `llm`, or `user`
- `evidence` — which snapshot fields support the proposal

When the user asks to *review*, walk `needs_review` in groups (by risk, by
subsystem prefix, by source) — do not dump 600 rows.

## Cautions

- **DKMS**: if `snapshot.dkms` is non-empty, every entry must rebuild against
  any new kernel. The CLI displays a DKMS panel and *refuses* `auto-*`
  autonomy unless `--force-dkms` is passed. Surface this to the user before
  proposing anything.
- **Secure Boot**: if `snapshot.boot.secure_boot` is `true`, a custom kernel
  needs MOK enrollment. Flag it.
- **Unresolved modaliases**: `resolution.unresolved_modaliases` is normal for
  `acpi:`, `platform:`, `serio:` — those don't go through `modules.alias`.
  Only worry if a `pci:` or `usb:` modalias is unresolved.
- **Unresolved modules**: when the resolver can't find a CONFIG symbol for a
  required module, *all* of that module's candidate symbols become
  load-bearing. This is the conservative fallback.
- **The agent is advisory**. Even at `auto-bold`, the load-bearing blocklist
  is enforced. Never bypass it.

## Models

Default: `anthropic:claude-sonnet-4-6`. Override with `--model anthropic:claude-opus-4-7` for higher quality, or `openai:gpt-...` if Anthropic is unavailable. The user has both `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` in `.env`.

## Cost / resume

LLM batches are cached at `<snapshot>/batches/<hash>.json`. The hash covers
`(model, service_tier, system_prompt_version, batch contents)` — same
inputs hit the cache, different inputs re-call the LLM. Interrupting
`propose` mid-run is safe: rerun and only the unfinished batches will hit
the API.

## Review artifacts

After `autokernel review`, two files appear in the snapshot dir:

* **review.json** — typed `ReviewSet` with `accepted`/`rejected`/`deferred`
  buckets. Every input proposal lands in exactly one bucket. Each
  `ReviewedProposal` records the rule that decided it (`rule="accept-recommended-not-high-risk"` etc.) and the reviewer identity.
* **auto.kfrag** — a Kconfig fragment listing only ACCEPTED changes
  (disables and demotions). Standard format; `merge_config.sh -m .config
  auto.kfrag && make olddefconfig` applies it. Header comments record
  provenance (snapshot, autonomy, model, timestamp, counts) — these are
  ignored by the kernel's tooling but useful for humans and audit.

After `autokernel apply`, `<DIR>/final.config` is a complete kernel
configuration ready to drop into a kernel source tree. The merge is pure
Python — no kernel sources required. The validation step uses the same
load-bearing set the policy filter computed during `propose`, so any
attempt to disable a critical symbol is caught here too (defense in
depth: the policy filter catches it on the proposal side, the merge
validator catches it on the application side).
