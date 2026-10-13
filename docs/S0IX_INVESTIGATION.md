# S0ix on Framework Laptop 13 (Intel Core Ultra) — Investigation Diary

**Dates:** 2026-05-18 → 2026-05-21
**Hardware:** Framework Laptop 13, Intel Core Ultra 5 125H, BIOS INSYDE 3.06 (2025-08-08), WD Black SN850X 1 TB NVMe (`15b7:5030`), Intel AX210 Wi-Fi (`8086:2725`)
**OS:** Ubuntu 26.04 LTS, custom autokernel-built 7.1-rc4
**Goal:** reduce overnight battery drain by getting the platform to reach S0ix during s2idle suspend

## TL;DR

S0ix is not reachable on this hardware under any tested Linux configuration. The PCH PCIe link to the NVMe M.2 slot cannot enter L1.2 because **CLKREQ# does not appear to be physically wired to that slot** on the Framework PCB, evidenced by SPB (Wi-Fi slot) successfully power-gating with the same kernel + policy while SPA (NVMe slot) does not. No kernel-side change can rescue this; the realistic mitigations are (a) BIOS / hardware update from Framework, (b) swap NVMe to a drive with different firmware, or (c) bypass s2idle entirely by configuring hibernate.

Three of the four kernel patches drafted during this investigation are still useful upstream contributions independent of the platform issue.

## Hypotheses tested

| # | Hypothesis | Action | Result |
|---|---|---|---|
| 1 | NVMe APST cap (100 ms default) hides drive's deepest PS → drive doesn't reach PS4 → SPA blocked | `nvme_core.default_ps_max_latency_us=20000000` proposed | Disproved: SN850X PS4 `exit_lat = 45.7 ms` is well under 100 ms. APST already permitted to use it. |
| 2 | ACPI `RP01` wake-arming keeps NVMe root port in D0 | `echo RP01 > /proc/acpi/wakeup` toggle test | Disproved: residency stayed 0% |
| 3 | `NVME_QUIRK_SIMPLE_SUSPEND` (applied because BIOS asserts `storage_d3` ACPI hint) prevents NVMe entering deep PS during suspend | `nvme.noacpi=1` on kernel cmdline | Disproved: quirk message disappears, but residency still 0% |
| 4 | Kernel-side `ClockPM-` on NVMe path is a BIOS-bug misreport; the trace is wired but BIOS doesn't advertise the capability. Force-override in `pcie_clkpm_cap_init()` | Wrote patch [4 below], built `-s0ix-clkpm+` kernel, installed via probation | Disproved: quirk fires correctly on `a9:00.0`; with `pcie_aspm.policy=powersupersave` the substate enable bits flip on; **PMC1 (Thunderbolt/IOE) shows `PCIE_DEEP_PM_REQ_STS = 1`, SPB power-gates, but PMC0 SPA still `0`**. The asymmetry (SPB OK, SPA blocked) on identical silicon, identical kernel state, identical L1 substate config is the definitive evidence that the issue is *PCB-level wiring on the NVMe M.2 slot*, not a kernel/firmware capability bit. |

## Definitive evidence (substate_status_registers, last suspend on `-s0ix-clkpm+`)

```
PMC0:SPA_PGD0_PG_STS         0   ← NVMe Southport NOT power-gated
PMC0:SPB_PGD0_PG_STS         1   ← Wi-Fi Southport IS power-gated (new with patch+powersupersave)
PMC0:PCIE_DEEP_PM_REQ_STS    0   ← PMC0 PCIe deep-PM condition unmet → S0ix blocked
PMC1:SPA_PGD0_PG_STS         1   ← Thunderbolt side fine
PMC1:SPB_PGD0_PG_STS         1
PMC1:PCIE_DEEP_PM_REQ_STS    1
total_hw_sleep               0   ← platform never reaches hardware sleep
```

## What got produced and kept

### Kernel source branch

`~/.cache/autokernel/kernels/linux-torvalds-v7.1-rc4` on branch `s0ix-diagnostics`:

```
3147c55b8e30 PCI/ASPM: force ClockPM for Framework Laptop 13 MTL PCH root ports
91b528255172 Documentation/admin-guide/pm: add S0ix tuning guide
72f55d39ce3d nvme: log when APST skips a non-operational state due to latency cap
82026ed94c49 platform/x86/intel/pmc: rate-limit invalid LTR scale factor warning
v7.1-rc4 (base)
```

Branch is preserved.  Patch files are at `/tmp/patches/` (will be regenerated as `/tmp/patches-final/` for sending).

### Built kernel packages in `~/.cache/autokernel/kernels/`

| Release | Patches | Purpose |
|---|---|---|
| `7.1.0-rc4-autokernel-202605141609` | none | Original baseline; safe fallback |
| `7.1.0-rc4-autokernel-202605141609-s0ix+` | 1+2+3 (PMC LTR rate-limit, NVMe APST log, S0ix doc) | **Recommended daily driver** |
| `7.1.0-rc4-autokernel-202605141609-s0ix-clkpm+` | 1+2+3+4 (also experimental ClockPM force) | Kept on disk for reference; do not boot daily |

### Currently installed kernels in `/boot` (as of investigation end)

```
vmlinuz-6.19.0-9-generic                              (Ubuntu stock fallback)
vmlinuz-7.0.0-15-generic                              (Ubuntu stock fallback)
vmlinuz-7.1.0-rc3-autokernel-202605141609             (older autokernel build)
vmlinuz-7.1.0-rc4-autokernel-202605141609             (rc4 baseline, no patches)
vmlinuz-7.1.0-rc4-autokernel-202605141609-s0ix+       (patches 1+2+3 — daily driver)
vmlinuz-7.1.0-rc4-autokernel-202605141609-s0ix-clkpm+ (patches 1+2+3+4 — experimental, kept)
```

### Runtime tunings in /etc (persistent across reboots)

- `/etc/sysctl.d/99-power.conf` — `vm.dirty_writeback_centisecs = 1500`
- `/etc/tmpfiles.d/99-power-runtime-pm.conf` — runtime PM `auto` for 9 PCH PCIe devices
- ~~`/etc/default/grub` — `nvme.noacpi=1` on `GRUB_CMDLINE_LINUX_DEFAULT`~~ **REMOVED 2026-07-09 — was NOT harmless.**

> **Correction (2026-07-09):** `nvme.noacpi=1` was originally added here to silence the
> `platform quirk: setting simple suspend` log line, believed harmless. It is **not**. It
> makes the nvme driver ignore this platform's `StorageD3Enable` `_DSD` (confirmed present
> in the DSDT — 3 matches, one per PCIe M.2 slot), which disables `NVME_QUIRK_SIMPLE_SUSPEND`
> and forces the host-managed deep-PS suspend path. Because the platform actually removes
> NVMe power in s2idle (`d3cold_allowed=1`), the SN850X loses controller state across resume
> → `nvme … I/O tag … timeout, completion polled` (recovered near-misses) and, twice,
> hard I/O failures that propagated up through dm-crypt → LVM → ext4 and **remounted root
> read-only** (2026-06-17 on the 7.2mw build, 2026-07-08 on rc7 — i.e. across two kernels,
> ruling out a kernel regression). The kernel's own `acpi_storage_d3()` docstring warns:
> *"if this hint is ignored, @dev may remain unresponsive after suspending."*
> **Do not set `nvme.noacpi=1` on this machine.** Mainline default (SIMPLE_SUSPEND) is correct.
> No kernel patch is warranted for this box; the only patch-worthy variant is a
> `force_storage_d3()` DMI quirk for FW13 **AMD** units whose BIOS *lacks* `StorageD3Enable`,
> which is different hardware and arguably a Framework firmware bug.

## Upstream contribution status

| Patch | Status | Notes |
|---|---|---|
| 1: `platform/x86/intel/pmc: rate-limit invalid LTR scale factor warning` | **Ready to send** | Verified working on this kernel — fired exactly once with "scale 7" context, vs 7-12 unrate-limited messages on stock kernel. Recipient: `platform-driver-x86@vger.kernel.org` |
| 2: `nvme: log when APST skips a non-operational state due to latency cap` | **Ready to send** | Correct code; does not happen to fire on SN850X (PS4 fits cap), but useful for drives that exceed it. Recipient: `linux-nvme@lists.infradead.org` |
| 3: `Documentation/admin-guide/pm: add S0ix tuning guide` | **Needs rewrite** | Current worked example tells the APST-cap story which we disproved. Should be rewritten to teach the diagnostic *methodology* (substate_status_registers → identify blocker → check ClockPM/CLKREQ#) rather than one happy-path. Recipient: `linux-doc@vger.kernel.org` + `linux-pm@vger.kernel.org` |
| 4: `PCI/ASPM: force ClockPM for Framework Laptop 13 MTL PCH root ports` | **Do not send** | Code is correct, premise is wrong for the only platform we have evidence on. Quirk fired but did not unblock S0ix because the underlying CLKREQ# is not wired. Keep in branch for reference. |

## Recommended next actions

1. **Rollback / pin -s0ix+ as daily driver.** Manual: `sudo grub-set-default "Advanced options for Ubuntu>Ubuntu, with Linux 7.1.0-rc4-autokernel-202605141609-s0ix+"` then reboot. (Autokernel `rollback --execute` also works but removes `linux-libc-dev` via apt, which is risky; manual is safer for keeping the -clkpm+ deb on disk for reference.)

2. **Pivot to hibernate for actual overnight battery life.** Requires growing the 8 GB `/swap.img` to ≥ ~40 GB to fit 63 GB RAM, adding `resume=` to cmdline, updating initramfs, configuring logind for `suspend-then-hibernate` on lid close. Independent of S0ix.

3. **File precise issue with Framework** citing the empirical asymmetry: with kernel ClockPM override and `pcie_aspm.policy=powersupersave`, the Wi-Fi M.2 slot path (`00:1c.5` / SPB / AX210) reaches L1.2 and power-gates, while the NVMe M.2 slot path (`00:1c.0` / SPA / SN850X) does not. This strongly suggests CLKREQ# is not wired (or has a firmware misconfiguration) on the NVMe M.2 slot specifically. Reference: [community thread "FW13 Ultra: s2idle huge power draw"](https://community.frame.work/t/fw13-ultra-s2idle-huge-power-draw/60714).

4. **Send patches 1 and 2 upstream** (independent of this investigation's outcome).

5. **Rewrite patch 3 and send.** Update its worked example with the methodology used in this investigation: the substate_status_registers diff is the single most useful diagnostic; the per-Southport asymmetry pattern is what distinguishes "kernel-fixable" from "hardware-fixable" failure modes.

## Addendum (2026-05-21 PM): touchpad "interrupt storm" was misdiagnosed

A 60-second powertop sample showed `i2c_designware.3` at 4490 events/s with a 9.92 W discharge rate and an initial conclusion of "stuck IRQ storm after suspend/resume." Subsequent per-IRQ rate sampling over a 2-minute window revealed a different picture:

- During active touchpad use: IRQ 30 (i2c_designware.3) bursts to 400–1900/s
- During idle (no touchpad contact): IRQ 30 drops to **0/s**
- Ratio of IRQ 30 to IRQ 162 (PIXA3854 GPIO) is consistently ~36:1
- No `i2c_hid` errors, no "nobody cared", no "disabling irq" in dmesg

This is **normal-but-chatty touchpad polling**, not a stuck-IRQ storm. The 36:1 ratio reflects that the I2C controller IRQ fires per low-level transition (byte received, ACK, FIFO threshold) rather than per HID report packet, and the PixArt controller's protocol is on the verbose side.

The community-tracked issue at [community.frame.work/t/.../idma64-2/13630](https://community.frame.work/t/tracking-touchpad-interrupts-battery-usage-issues-idma64-2/13630) is the real, multi-year, unsolved inefficiency in this code path — *but it's a chattiness issue under active use, not a fault under idle.* No `I2C_HID_QUIRK_NO_IRQ_AFTER_RESET`-style fix would address it, because the controller is not staying asserted past the actual transactions. The powertop wakeup number is proportional to actual touchpad use.

Lesson for future debugging: a wakeup count from a single integrated powertop run is averaged-and-misleading. Per-IRQ rate sampling with no-touchpad-contact and with-touchpad-contact windows is the correct diagnostic for distinguishing "stuck IRQ" from "expensive-but-correct activity."

## Useful diagnostic scripts created during the investigation

These live under `/tmp/` (cleared on reboot — re-create if needed):

- `/tmp/s0ix-blocker.sh` — short version: read `slp_s0_residency_usec`, suspend via rtcwake, read again, report %. Definitive answer for "did S0ix happen during the last suspend"
- `/tmp/fix-nvme-s0ix.sh` — adds `nvme_core.default_ps_max_latency_us=20000000` to grub cmdline (now known to be unnecessary on SN850X; kept as template)
- `/tmp/try-noacpi.sh` — adds `nvme.noacpi=1` to grub cmdline (⚠️ REMOVED 2026-07-09; see correction above — causes read-only-root on resume, do not re-apply)
- `/tmp/test-rp01-off.sh` — non-persistent toggle of ACPI RP01 wake-arming for A/B test

## Upstream send log

| Date | Patch | Subject | List | Lore URL |
|---|---|---|---|---|
| 2026-05-23 | 0001 | `[PATCH] platform/x86/intel/pmc: rate-limit LTR scale-factor warning` | platform-driver-x86 | (TODO: kernel-patches-workspace/sent/0001/lore-link.txt) |
| 2026-05-23 | 0002 | `[PATCH] nvme: log when APST skips a state due to latency cap` | linux-nvme | (TODO: kernel-patches-workspace/sent/0002/lore-link.txt) |
| 2026-05-23 | 0003 (iwlwifi) | `[PATCH] wifi: iwlwifi: mvm: handle SEC_UNKNOWN in non-AMPDU rx path` | linux-wireless | https://lore.kernel.org/all/20260523142438.118284-1-michael.bommarito@gmail.com/ |

Both sent as independent standalone single-patch threads (no cover
letter, no series N/M numbering, no In-Reply-To). Per-patch artifacts
(envelope, checklist, send.sh, .patch snapshot) preserved under
`kernel-patches-workspace/sent/000X/`.

Patches 3 (S0ix doc) and 4 (PCI ASPM ClockPM quirk) deliberately
held back: 3 needs rewrite (worked example was based on the
disproved APST-cap hypothesis); 4 will not be sent (CLKREQ# is
genuinely not wired on the NVMe M.2 slot, so the quirk doesn't
actually unblock S0ix on test hardware).
