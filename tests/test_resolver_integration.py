"""End-to-end resolver tests on synthetic fixtures.

Closes the regression vector identified by the Slice 0.1 review: a module
like ``i915`` was previously mapped to ``CONFIG_I915`` (does not exist),
silently dropped from ``required_configs``, and therefore left out of the
load-bearing blocklist — at ``auto-bold``, the LLM could trim it.

These tests pin the path-aware mapping in place. They do NOT depend on
``modinfo`` being installed: the fixture's loaded modules' source paths
are looked up against the fixture's own running config.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from autokernel.kconfig_map import resolve_module_to_config
from autokernel.models import Snapshot
from autokernel.policy import compute_load_bearing
from autokernel.resolve import (
    _running_config_symbols,
    candidate_trims,
    focused_candidate_trims,
    resolve,
)
from autokernel.snapshot import load


# ── direct candidate-generator checks against fixture configs ───────────────


def test_intel_laptop_i915_maps_to_drm_i915(intel_laptop: Snapshot):
    running = _running_config_symbols(intel_laptop.running_config_path)
    cfg = resolve_module_to_config("i915", "drivers/gpu/drm/i915/i915", running)
    assert cfg == "CONFIG_DRM_I915"


def test_intel_laptop_iwlwifi_maps_to_iwlwifi(intel_laptop: Snapshot):
    running = _running_config_symbols(intel_laptop.running_config_path)
    cfg = resolve_module_to_config(
        "iwlwifi", "drivers/net/wireless/intel/iwlwifi/iwlwifi", running
    )
    assert cfg == "CONFIG_IWLWIFI"


def test_intel_laptop_btrfs_maps_to_btrfs_fs(intel_laptop: Snapshot):
    running = _running_config_symbols(intel_laptop.running_config_path)
    cfg = resolve_module_to_config("btrfs", "fs/btrfs/btrfs", running)
    assert cfg == "CONFIG_BTRFS_FS"


# ── policy: active NIC driver actually marked load-bearing ──────────────────


def test_active_nic_driver_is_load_bearing(intel_laptop: Snapshot):
    """The Intel laptop fixture has wlp0s20f3 UP with iwlwifi.

    With the broken pre-Slice-0.2 resolver, CONFIG_IWLWIFI may or may not
    have been added. With the path-aware mapping the snapshot fixture's
    iwlwifi (no source path because we don't run modinfo against synthetic
    fixtures) falls back to its bare-name candidate CONFIG_IWLWIFI, which
    the fixture's running_config has at =m. The unresolved-module
    protection fires either way. Verify CONFIG_IWLWIFI is in the
    load-bearing set.
    """
    res = resolve(intel_laptop)
    lb = compute_load_bearing(intel_laptop, res)
    is_lb, reason = lb.contains("CONFIG_IWLWIFI")
    assert is_lb, f"CONFIG_IWLWIFI missing from load-bearing set; reason={reason!r}"


def test_active_nic_driver_is_load_bearing_amd(amd_desktop: Snapshot):
    res = resolve(amd_desktop)
    lb = compute_load_bearing(amd_desktop, res)
    is_lb, _ = lb.contains("CONFIG_R8169")
    assert is_lb


# ── unresolved modules conservatively protect their candidates ──────────────


def test_unresolved_module_protected_via_candidates(intel_laptop: Snapshot):
    """If the resolver can't find a CONFIG for a required module, ALL of
    its candidate symbols become load-bearing — protecting against the
    resolver missing the right one."""
    res = resolve(intel_laptop)
    # Insert a synthetic unresolved module to exercise the fallback path.
    res.unresolved_modules.add("totally_made_up_module")
    lb = compute_load_bearing(intel_laptop, res)
    # The naive candidate must be in the LB set.
    assert lb.contains("CONFIG_TOTALLY_MADE_UP_MODULE")[0]


def test_focused_candidate_trims_uses_modules_dep(tmp_path, intel_laptop: Snapshot):
    modules_dep = tmp_path / "modules.dep"
    modules_dep.write_text(
        "\n".join(
            [
                "kernel/drivers/counter/104-quad-8.ko.zst:",
                "kernel/drivers/watchdog/60xx_wdt.ko.zst:",
            ]
        )
        + "\n"
    )
    snap = intel_laptop.model_copy(update={"modules_dep_path": modules_dep})
    res = resolve(snap)
    broad = candidate_trims(snap, res)
    focused = focused_candidate_trims(snap, res, broad_candidates=broad)

    assert len(focused) < len(broad)
    assert "CONFIG_104_QUAD_8" in focused
    assert "CONFIG_60XX_WDT" in focused


def test_laptop_audio_stack_is_load_bearing(intel_laptop: Snapshot):
    res = resolve(intel_laptop)
    lb = compute_load_bearing(intel_laptop, res)

    for sym in (
        "CONFIG_SND_HDA_INTEL",
        "CONFIG_SND_HDA_CODEC_REALTEK",
        "CONFIG_SND_SOC_SOF_INTEL_MTL",
        "CONFIG_SND_SOC_INTEL_SOUNDWIRE_SOF_MACH",
        "CONFIG_SND_USB_AUDIO",
    ):
        is_lb, reason = lb.contains(sym)
        assert is_lb, f"{sym} not load-bearing; reason={reason!r}"


def test_headless_without_audio_can_still_trim_audio(tmp_path):
    src = Path(__file__).parent / "fixtures" / "intel_laptop"
    snapdir = tmp_path / "headless"
    shutil.copytree(src, snapdir)

    blocks = (snapdir / "lspci_vmmnk").read_text().split("\n\n")
    blocks = [b for b in blocks if "Class:\t0401" not in b]
    (snapdir / "lspci_vmmnk").write_text("\n\n".join(blocks) + "\n")

    lsmod_lines = []
    for line in (snapdir / "lsmod").read_text().splitlines():
        first = line.split(None, 1)[0] if line.split() else ""
        if first.startswith("snd") or first.startswith("soundwire"):
            continue
        lsmod_lines.append(line)
    (snapdir / "lsmod").write_text("\n".join(lsmod_lines) + "\n")

    (snapdir / "dmi_id").write_text(
        "sys_vendor\tDell Inc.\n"
        "product_name\tPowerEdge R760\n"
        "product_family\tPowerEdge\n"
        "chassis_type\t23\n"
    )
    (snapdir / "software_features").write_text("")
    (snapdir / "asound_cards").write_text("--- no soundcards ---\n")
    (snapdir / "asound_pcm").write_text("")
    (snapdir / "dev_snd").write_text("")
    (snapdir / "sys_class_sound").write_text("")

    snap = load(snapdir)
    assert snap.audio.useful is False

    res = resolve(snap)
    trims = set(candidate_trims(snap, res))
    assert "CONFIG_SND_USB_AUDIO" in trims
    assert "CONFIG_SND_HDA_CODEC_REALTEK" in trims
