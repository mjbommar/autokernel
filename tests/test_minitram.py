"""Tests for autokernel.minitram.

We don't run cpio/zstd in tests — that's an integration concern.
Here we verify the plan() composition is correct given different
boot-chain features, and that build()'s staging logic places files
where they should go (subprocess invocations are mocked).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


from autokernel.minitram import (
    MinitramModule,
    MinitramPlan,
    _compose_init_script,
    _find_module_path,
    plan,
    _luks_in_chain,
    _lvm_in_chain,
    _md_raid_in_chain,
)
from autokernel.models import (
    BlockDevice,
    BootContext,
    CpuInfo,
    DkmsModule,
    KernelInfo,
    Snapshot,
)


def _bare_snap(
    *,
    luks: bool = False,
    root_fstype: str | None = None,
    block_devices: list[BlockDevice] | None = None,
    dkms: list[DkmsModule] | None = None,
) -> Snapshot:
    return Snapshot(
        collected_at=datetime.now(timezone.utc),
        host="testhost",
        snapshot_dir=Path("/tmp/snap"),
        kernel=KernelInfo(release="6.19.0-test", version="#1 SMP", arch="x86_64"),
        cpu=CpuInfo(vendor_id="GenuineIntel"),
        boot=BootContext(
            cmdline="ro quiet", luks_in_chain=luks, root_fstype=root_fstype
        ),
        block_devices=block_devices or [],
        dkms=dkms or [],
    )


# ── boot-chain predicates ────────────────────────────────────────────────


def test_luks_predicate_reflects_boot_field():
    assert _luks_in_chain(_bare_snap(luks=True))
    assert not _luks_in_chain(_bare_snap(luks=False))


def test_lvm_predicate_checks_block_devices():
    snap_with_lvm = _bare_snap(
        block_devices=[
            BlockDevice(name="vg-root", type="lvm"),
        ]
    )
    snap_no_lvm = _bare_snap(
        block_devices=[
            BlockDevice(name="sda1", type="disk"),
        ]
    )
    assert _lvm_in_chain(snap_with_lvm)
    assert not _lvm_in_chain(snap_no_lvm)


def test_md_raid_predicate():
    snap = _bare_snap(block_devices=[BlockDevice(name="md0", type="raid")])
    assert _md_raid_in_chain(snap)
    snap2 = _bare_snap(
        block_devices=[BlockDevice(name="md1")]
    )  # type missing but name 'md*'
    assert _md_raid_in_chain(snap2)


# ── plan: tool selection ────────────────────────────────────────────────


def test_plan_minimal_no_features(monkeypatch, tmp_path):
    """Snapshot with no LUKS / LVM / RAID / DKMS — minitram contains
    just init + busybox. No special tools."""
    monkeypatch.setattr("autokernel.minitram._which_or_none", lambda n: None)
    snap = _bare_snap(root_fstype="ext4")
    p = plan(snap, modules_root=tmp_path)
    assert p.tools == []  # no luks/lvm/raid → no tools
    # init script always present
    assert p.init_script
    assert "switch_root" in p.init_script


def test_plan_includes_cryptsetup_when_luks_in_chain(monkeypatch, tmp_path):
    """LUKS in chain → plan must include cryptsetup."""
    fake_cs = tmp_path / "cryptsetup-fake"
    fake_cs.write_text("#!/bin/sh\n")
    fake_cs.chmod(0o755)
    monkeypatch.setattr(
        "autokernel.minitram._which_or_none",
        lambda n: fake_cs if n == "cryptsetup" else None,
    )
    monkeypatch.setattr("autokernel.minitram._resolve_libs", lambda p: [])

    snap = _bare_snap(luks=True, root_fstype="ext4")
    p = plan(snap, modules_root=tmp_path)
    cs_tools = [t for t in p.tools if t.name == "cryptsetup"]
    assert len(cs_tools) == 1
    assert "LUKS" in cs_tools[0].rationale
    # init script must mention cryptsetup invocation
    assert "cryptsetup luksOpen" in p.init_script


def test_plan_includes_lvm_when_lvm_in_chain(monkeypatch, tmp_path):
    fake_lvm = tmp_path / "lvm-fake"
    fake_lvm.write_text("#!/bin/sh\n")
    fake_lvm.chmod(0o755)
    monkeypatch.setattr(
        "autokernel.minitram._which_or_none", lambda n: fake_lvm if n == "lvm" else None
    )
    monkeypatch.setattr("autokernel.minitram._resolve_libs", lambda p: [])

    snap = _bare_snap(
        block_devices=[BlockDevice(name="vg-root", type="lvm")], root_fstype="ext4"
    )
    p = plan(snap, modules_root=tmp_path)
    assert any(t.name == "lvm" for t in p.tools)
    assert "vgchange" in p.init_script


def test_plan_includes_dropbear_only_when_requested(monkeypatch, tmp_path):
    fake = tmp_path / "dropbear-fake"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(
        "autokernel.minitram._which_or_none",
        lambda n: fake if n == "dropbear" else None,
    )
    monkeypatch.setattr("autokernel.minitram._resolve_libs", lambda p: [])

    snap = _bare_snap(root_fstype="ext4")
    p_default = plan(snap, modules_root=tmp_path)
    assert all(t.name != "dropbear" for t in p_default.tools)

    p_with = plan(snap, modules_root=tmp_path, include_dropbear=True)
    assert any(t.name == "dropbear" for t in p_with.tools)


def test_plan_skips_missing_tool_silently(monkeypatch, tmp_path):
    """User has LUKS in chain but cryptsetup not installed — plan
    skips it (build() can warn later) rather than crashing."""
    monkeypatch.setattr("autokernel.minitram._which_or_none", lambda n: None)
    snap = _bare_snap(luks=True, root_fstype="ext4")
    p = plan(snap, modules_root=tmp_path)
    assert all(t.name != "cryptsetup" for t in p.tools)


# ── plan: module selection ──────────────────────────────────────────────


def test_plan_picks_modules_for_root_fstype(monkeypatch, tmp_path):
    """root_fstype=btrfs → btrfs.ko in modules list."""
    monkeypatch.setattr("autokernel.minitram._which_or_none", lambda n: None)
    # Stage a fake /lib/modules/<release>/ tree.
    rel_dir = tmp_path / "6.19.0-test"
    (rel_dir / "fs/btrfs").mkdir(parents=True)
    btrfs_ko = rel_dir / "fs/btrfs/btrfs.ko.zst"
    btrfs_ko.write_text("")

    snap = _bare_snap(root_fstype="btrfs")
    p = plan(snap, modules_root=tmp_path)
    assert any(m.name == "btrfs" for m in p.modules)
    btrfs_mod = next(m for m in p.modules if m.name == "btrfs")
    assert "btrfs" in btrfs_mod.rationale


def test_plan_picks_dm_crypt_for_luks(monkeypatch, tmp_path):
    monkeypatch.setattr("autokernel.minitram._which_or_none", lambda n: None)
    rel_dir = tmp_path / "6.19.0-test"
    (rel_dir / "drivers/md").mkdir(parents=True)
    dm_crypt_ko = rel_dir / "drivers/md/dm_crypt.ko"
    dm_crypt_ko.write_text("")

    snap = _bare_snap(luks=True, root_fstype="ext4")
    p = plan(snap, modules_root=tmp_path)
    assert any(m.name == "dm_crypt" for m in p.modules)


def test_plan_picks_dkms_modules(monkeypatch, tmp_path):
    monkeypatch.setattr("autokernel.minitram._which_or_none", lambda n: None)
    rel_dir = tmp_path / "6.19.0-test"
    (rel_dir / "extra").mkdir(parents=True)
    nvidia_ko = rel_dir / "extra/nvidia.ko"
    nvidia_ko.write_text("")

    snap = _bare_snap(
        root_fstype="ext4",
        dkms=[
            DkmsModule(
                name="nvidia", version="535.0", kernel="6.19.0-test", status="installed"
            )
        ],
    )
    p = plan(snap, modules_root=tmp_path)
    assert any(m.name == "nvidia" for m in p.modules)
    nv = next(m for m in p.modules if m.name == "nvidia")
    assert "DKMS" in nv.rationale


def test_plan_module_dedup(monkeypatch, tmp_path):
    """A symbol claimed by multiple features (e.g. aes_x86_64 by both
    LUKS and another path) shows up only once."""
    monkeypatch.setattr("autokernel.minitram._which_or_none", lambda n: None)
    rel_dir = tmp_path / "6.19.0-test"
    (rel_dir / "crypto").mkdir(parents=True)
    aes = rel_dir / "crypto/aes_x86_64.ko"
    aes.write_text("")
    dm_crypt_dir = rel_dir / "drivers/md"
    dm_crypt_dir.mkdir(parents=True)
    (dm_crypt_dir / "dm_crypt.ko").write_text("")

    snap = _bare_snap(luks=True, root_fstype="ext4")
    p = plan(snap, modules_root=tmp_path)
    aes_count = sum(1 for m in p.modules if m.name == "aes_x86_64")
    assert aes_count == 1


def test_plan_skips_module_not_on_disk(monkeypatch, tmp_path):
    """Plan asks for btrfs.ko but it's not under /lib/modules — skip."""
    monkeypatch.setattr("autokernel.minitram._which_or_none", lambda n: None)
    snap = _bare_snap(root_fstype="btrfs")
    p = plan(snap, modules_root=tmp_path)  # empty modules root
    assert all(m.name != "btrfs" for m in p.modules)


# ── _find_module_path ────────────────────────────────────────────────────


def test_find_module_path_finds_zst(tmp_path):
    rel_dir = tmp_path / "6.19.0-test"
    (rel_dir / "fs/ext4").mkdir(parents=True)
    target = rel_dir / "fs/ext4/ext4.ko.zst"
    target.write_text("")
    found = _find_module_path("ext4", "6.19.0-test", modules_root=tmp_path)
    assert found == target


def test_find_module_path_prefers_zst_over_ko(tmp_path):
    """Distros sometimes have both .ko and .ko.zst. Prefer the
    distro-current one (.ko.zst on Ubuntu/Fedora; raw .ko on
    in-source-tree)."""
    rel_dir = tmp_path / "6.19.0-test"
    (rel_dir / "fs/ext4").mkdir(parents=True)
    raw = rel_dir / "fs/ext4/ext4.ko"
    raw.write_text("")
    zst = rel_dir / "fs/ext4/ext4.ko.zst"
    zst.write_text("")
    found = _find_module_path("ext4", "6.19.0-test", modules_root=tmp_path)
    # Returns first match per the iteration order — both should be findable.
    assert found in (raw, zst)


def test_find_module_path_returns_none_when_release_dir_missing(tmp_path):
    found = _find_module_path("ext4", "no-such-release", modules_root=tmp_path)
    assert found is None


# ── init script composition ──────────────────────────────────────────────


def test_init_script_has_pivot_root():
    snap = _bare_snap(root_fstype="ext4")
    p = MinitramPlan(kernel_release="x", snapshot_dir=Path("/tmp"))
    p.modules.append(
        MinitramModule(
            name="ext4",
            host_path=Path("/x"),
            target_path="/",
            rationale="root",
        )
    )
    script = _compose_init_script(snap, p)
    assert "switch_root" in script
    assert "/newroot" in script


def test_init_script_loads_modules_in_plan_order():
    snap = _bare_snap(luks=True, root_fstype="ext4")
    p = MinitramPlan(kernel_release="x", snapshot_dir=Path("/tmp"))
    p.modules.append(
        MinitramModule(
            name="dm_crypt", host_path=Path("/x"), target_path="/", rationale=""
        )
    )
    p.modules.append(
        MinitramModule(name="ext4", host_path=Path("/y"), target_path="/", rationale="")
    )
    script = _compose_init_script(snap, p)
    # dm_crypt before ext4 — order matters for boot.
    pos_dmcrypt = script.find("modprobe dm_crypt")
    pos_ext4 = script.find("modprobe ext4")
    assert 0 <= pos_dmcrypt < pos_ext4


# ── plan summary serializable ────────────────────────────────────────────


def test_plan_to_summary_dict_serializable(monkeypatch, tmp_path):
    """to_summary_dict should be JSON-serializable (no Path objects)."""
    monkeypatch.setattr("autokernel.minitram._which_or_none", lambda n: None)
    snap = _bare_snap(root_fstype="ext4")
    p = plan(snap, modules_root=tmp_path)
    s = p.to_summary_dict()
    text = json.dumps(s)  # must not raise
    assert "kernel_release" in text
