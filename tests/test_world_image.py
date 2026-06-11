"""Tests for the world image builder (W5 Path A). No subprocess: argv
composition, hook contents, size heuristic, and serial-verdict parsing."""

from __future__ import annotations

from pathlib import Path

from autokernel.world import image as image_mod
from autokernel.world.models import BaseRelease


def _base() -> BaseRelease:
    return BaseRelease(
        distro_id="ubuntu",
        suite="resolute",
        mirror="http://archive.ubuntu.com/ubuntu",
        components=["main", "universe"],
    )


def _argv() -> list[str]:
    return image_mod.assemble_argv(
        base=_base(),
        repo_dir=Path("/w/repo"),
        keyring=Path("/w/repo/autokernel-world-keyring.gpg"),
        out_tar=Path("/w/image/rootfs.tar"),
        includes=image_mod.DEFAULT_INCLUDES,
    )


def test_assemble_argv_pins_world_repo_and_upgrades():
    joined = "\n".join(_argv())
    assert "--mode=unshare" in joined
    assert "--variant=required" in joined
    assert "sync-in /w/repo /srv/world-repo" in joined
    assert "Pin-Priority: 1001" in joined
    assert "apt-get -y dist-upgrade" in joined
    # The init set: ring 0 has no init by design.
    assert "apt-get -y install systemd systemd-sysv udev kmod login passwd" in joined


def test_assemble_argv_cleans_host_references():
    joined = "\n".join(_argv())
    assert 'rm -rf "$1/srv/world-repo"' in joined
    assert "world.list" in joined  # removed at the end too


def test_sentinel_unit_prints_and_powers_off_only_in_test_mode():
    assert image_mod.BOOT_SENTINEL in image_mod._SENTINEL_UNIT
    assert "autokernel.boottest /proc/cmdline" in image_mod._SENTINEL_UNIT
    assert "WantedBy=multi-user.target" in image_mod._SENTINEL_UNIT


def test_materialize_file_hooks_stages_files(tmp_path):
    argv = _argv()
    out = image_mod._materialize_file_hooks(argv, tmp_path)
    joined = "\n".join(out)
    assert "upload-autologin-dropin" not in joined
    assert "upload-sentinel-unit" not in joined
    assert (tmp_path / "autologin.conf").exists()
    assert (tmp_path / "world-boot-ok.service").exists()
    assert f"copy-in {tmp_path / 'world-boot-ok.service'} /etc/systemd/system" in joined


def test_image_size_heuristic(tmp_path):
    tar = tmp_path / "r.tar"
    tar.write_bytes(b"\0" * (100 * 1024 * 1024))  # 100 MB
    assert image_mod.image_size_mb(tar) == 396  # 100*1.4 + 256
    assert image_mod.image_size_mb(tar, explicit_mb=512) == 512


def test_analyze_world_serial_verdicts():
    ok, reason = image_mod.analyze_world_serial(
        f"systemd[1]: Reached target Multi-User\n{image_mod.BOOT_SENTINEL}\n"
    )
    assert ok and "sentinel" in reason

    ok, reason = image_mod.analyze_world_serial(
        "Kernel panic - not syncing: VFS: Unable to mount root fs"
    )
    assert not ok and "Kernel panic" in reason

    ok, reason = image_mod.analyze_world_serial("systemd[1]: booting…")
    assert not ok and "timeout" in reason
