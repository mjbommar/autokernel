# Hardware Boot Smoke Test

Use `scripts/hardware-reboot-smoke.sh` to build an LLM-optimized kernel for
the current machine, verify it in a VM, and optionally install it for a
one-shot GRUB boot.

There is also a Python/Rich orchestrator with the same defaults:

```bash
uv run python scripts/hardware-reboot-smoke.py
```

Use the Python version when you want live monitoring during the slow build:
it keeps sudo alive, tails the autokernel build logs, counts active compiler
jobs and object files, and shows package artifacts as they appear.

The hardware smoke scripts default to `--boot-test-method qemu` because it is
the most stable install gate for the freshly built `bzImage`. `virtme` remains
available with `--boot-test-method virtme`.

The script is intentionally conservative about system mutation:

- default run builds and VM boot-tests only; it does not touch `/boot`
- `--install` installs the package and arms a one-shot GRUB entry
- `--install --reboot` does the same, then reboots immediately
- it asks `sudo -v` once and keeps the sudo timestamp alive during long builds
- NVIDIA driver handling defaults to `--nvidia auto`: if NVIDIA hardware and
  driver usage are detected, install adds a matching DKMS driver package,
  builds the modules for the custom kernel release, verifies them, and refreshes
  initramfs before GRUB is armed. Use `--nvidia open`, `--nvidia proprietary`,
  or `--nvidia off` to override.

## Default Optimizer Path

By default, the script uses the LLM optimizer:

```bash
autokernel propose SNAPSHOT \
  --dimension choices,toggles,tunables \
  --candidate-scope focused \
  --kernel-source SOURCE \
  --llm-mode auto \
  --threat balanced \
  --modules distro \
  --aggression aggressive
```

This means:

- the LLM is used for bounded, high-impact choice, toggle, and tunable decisions
- module trimming for the smoke boot is handled deterministically by the later
  `build --localmodconfig` step
- the module-trim LLM path remains available with `--dimension all` or
  `--dimension modules,...`; it is uncapped by default, and `--max-candidates`
  is only an explicit cost guard, not a substitute for evidence-derived
  candidate selection
- choice, toggle, and tunable dimensions run against the target kernel source
- choices, toggles, and tunables are allowlisted to high-impact knobs
- security stays balanced rather than permissive
- the current `=y`/`=m` balance is preserved unless the LLM has a clear reason
- lower-confidence-but-defensible changes are proposed for review

Use `--skip-llm` only for debugging the deterministic path.
Pass `--modules monolithic` only when you explicitly want the LLM to bias
load-bearing modules toward built-ins and potentially reduce initramfs reliance.

The module-trim LLM path needs an evidence-derived candidate generator before
it should be the default for hardware boot smoke tests. The detailed gap list
and implementation plan are in [LLM_EFFICIENCY_PLAN.md](LLM_EFFICIENCY_PLAN.md).

## Build And Test Only

```bash
scripts/hardware-reboot-smoke.sh
```

Artifacts land under:

```text
~/.local/share/autokernel/hardware-boot/
```

The script also exports:

```text
TMPDIR=~/.local/share/autokernel/hardware-boot/tmp
```

so compiler and packaging temporary files do not spill into `/tmp`.

The script stamps a unique `CONFIG_LOCALVERSION` so the built kernel release is
distinct from the running distro kernel.

## Install For One-Shot Boot

After reviewing the build and boot-test output:

```bash
scripts/hardware-reboot-smoke.sh --install --no-deps
```

The script derives the GRUB entry from the currently running Ubuntu entry and
writes it to:

```text
<snapshot>/grub-one-shot-entry
```

If entry detection is wrong, pass it explicitly:

```bash
scripts/hardware-reboot-smoke.sh --install --kernel-entry 'Advanced options for Ubuntu>Ubuntu, with Linux 6.19.0-autokernel-YYYYmmddHHMM'
```

## Install And Reboot

```bash
scripts/hardware-reboot-smoke.sh --install --reboot --yes --no-deps
```

GRUB should boot the new kernel once. If it fails, the next boot should fall
back to the previous default.

After a successful boot, promote it permanently with the `--commit` command
printed by the script. If you need to undo the install, use the printed
`autokernel rollback` command.
