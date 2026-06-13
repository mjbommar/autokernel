"""Unit tests for individual preflight checks (no live system probing)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


from autokernel import preflight as pf
from autokernel.distro import Family, parse_os_release, spec_for


def _ctx_for_family(family: Family, snapshot=None) -> pf.CheckContext:
    info = parse_os_release(
        {
            Family.DEBIAN: 'ID=ubuntu\nID_LIKE=debian\nPRETTY_NAME="Ubuntu Test"\n',
            Family.FEDORA: 'ID=fedora\nPRETTY_NAME="Fedora Test"\n',
            Family.ARCH: 'ID=arch\nPRETTY_NAME="Arch Test"\n',
            Family.UNKNOWN: "ID=mystery\n",
        }[family]
    )
    return pf.CheckContext(distro=info, spec=spec_for(info), snapshot=snapshot)


# ── distro_recognized ──────────────────────────────────────────────────────


def test_distro_recognized_pass_for_known():
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_distro_recognized(ctx)
    assert r.severity == pf.Severity.PASS


def test_distro_recognized_warn_for_unknown():
    ctx = _ctx_for_family(Family.UNKNOWN)
    r = pf.check_distro_recognized(ctx)
    assert r.severity == pf.Severity.WARN


# ── python_version ──────────────────────────────────────────────────────────


def test_python_version_pass_on_modern_python():
    """We're running 3.12+ in CI, so this must pass — pin the contract."""
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_python_version(ctx)
    assert r.severity == pf.Severity.PASS


# ── free_disk_space ─────────────────────────────────────────────────────────


class _FakeStatVfs:
    def __init__(self, free_gb: float):
        self.f_bavail = int(free_gb * 1024**3 / 4096)
        self.f_frsize = 4096


def test_free_disk_space_pass_when_plenty(monkeypatch):
    monkeypatch.setattr(pf.os, "statvfs", lambda p: _FakeStatVfs(50))
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_free_disk_space(ctx, "/tmp")
    assert r.severity == pf.Severity.PASS
    assert "50" in r.message


def test_free_disk_space_warn_below_warn_threshold(monkeypatch):
    monkeypatch.setattr(pf.os, "statvfs", lambda p: _FakeStatVfs(15))
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_free_disk_space(ctx, "/tmp")
    assert r.severity == pf.Severity.WARN


def test_free_disk_space_fail_below_fail_threshold(monkeypatch):
    monkeypatch.setattr(pf.os, "statvfs", lambda p: _FakeStatVfs(2))
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_free_disk_space(ctx, "/tmp")
    assert r.severity == pf.Severity.FAIL


# ── free_ram ────────────────────────────────────────────────────────────────


def test_free_ram_pass(monkeypatch, tmp_path: Path):
    fake = tmp_path / "meminfo"
    fake.write_text("MemTotal:       16777216 kB\n")  # 16 GB
    real_open = open

    def _fake_open(path, *a, **kw):
        if path == "/proc/meminfo":
            return real_open(fake, *a, **kw)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", _fake_open)
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_free_ram(ctx)
    assert r.severity == pf.Severity.PASS


def test_free_ram_warn_below_4gb(monkeypatch, tmp_path: Path):
    fake = tmp_path / "meminfo"
    fake.write_text("MemTotal:       3145728 kB\n")  # 3 GB
    real_open = open

    def _fake_open(path, *a, **kw):
        if path == "/proc/meminfo":
            return real_open(fake, *a, **kw)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", _fake_open)
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_free_ram(ctx)
    assert r.severity == pf.Severity.WARN


# ── build_tools — distro-aware install hints ────────────────────────────────


def test_build_tools_pass_when_all_present(monkeypatch):
    monkeypatch.setattr(pf.shutil, "which", lambda c: f"/usr/bin/{c}")
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_build_tools(ctx)
    assert r.severity == pf.Severity.PASS


def test_build_tools_fail_with_debian_hint(monkeypatch):
    """Missing flex/bison should produce an `apt install -y` hint."""

    def _which(c):
        return None if c in {"flex", "bison"} else f"/usr/bin/{c}"

    monkeypatch.setattr(pf.shutil, "which", _which)
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_build_tools(ctx)
    assert r.severity == pf.Severity.FAIL
    assert "flex" in r.message and "bison" in r.message
    assert "apt install" in (r.fix_hint or "")


def test_build_tools_fail_with_fedora_hint(monkeypatch):
    def _which(c):
        return None if c == "flex" else f"/usr/bin/{c}"

    monkeypatch.setattr(pf.shutil, "which", _which)
    ctx = _ctx_for_family(Family.FEDORA)
    r = pf.check_build_tools(ctx)
    assert r.severity == pf.Severity.FAIL
    assert "dnf install" in (r.fix_hint or "")


def test_build_tools_fail_with_arch_hint(monkeypatch):
    def _which(c):
        return None if c == "make" else f"/usr/bin/{c}"

    monkeypatch.setattr(pf.shutil, "which", _which)
    ctx = _ctx_for_family(Family.ARCH)
    r = pf.check_build_tools(ctx)
    assert r.severity == pf.Severity.FAIL
    assert "pacman" in (r.fix_hint or "")
    # Arch's make comes from base-devel
    assert "base-devel" in (r.fix_hint or "")


# ── recommended_tools ───────────────────────────────────────────────────────


def test_recommended_tools_pass_when_present(monkeypatch):
    monkeypatch.setattr(pf.shutil, "which", lambda c: f"/usr/bin/{c}")
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_recommended_tools(ctx)
    assert r.severity == pf.Severity.PASS


def test_recommended_tools_warn_when_pahole_missing(monkeypatch):
    def _which(c):
        return None if c == "pahole" else f"/usr/bin/{c}"

    monkeypatch.setattr(pf.shutil, "which", _which)
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_recommended_tools(ctx)
    assert r.severity == pf.Severity.WARN
    # Debian: dwarves package provides pahole
    assert "dwarves" in (r.fix_hint or "")


def test_recommended_tools_arch_uses_pahole_pkg_name(monkeypatch):
    def _which(c):
        return None if c == "pahole" else f"/usr/bin/{c}"

    monkeypatch.setattr(pf.shutil, "which", _which)
    ctx = _ctx_for_family(Family.ARCH)
    r = pf.check_recommended_tools(ctx)
    # Arch ships pahole as `pahole`, not dwarves
    assert "pahole" in (r.fix_hint or "")


# ── kernel_dev_libs ─────────────────────────────────────────────────────────


def test_kernel_dev_libs_pass_when_query_finds_them(monkeypatch):
    """Mock dpkg-query to report all libs installed."""

    def _fake_run(*args, **kwargs):
        class R:
            returncode = 0
            stdout = (
                "libssl-dev install ok installed\n"
                "libelf-dev install ok installed\n"
                "libdw-dev install ok installed\n"
                "libncurses-dev install ok installed\n"
            )

        return R()

    monkeypatch.setattr(pf.subprocess, "run", _fake_run)
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_kernel_dev_libs(ctx)
    assert r.severity == pf.Severity.PASS


def test_kernel_dev_libs_query_unavailable_skips(monkeypatch):
    """If dpkg-query is not on PATH (e.g. running outside Debian), the
    check returns conservatively (no FAIL)."""

    def _fake_run(*args, **kwargs):
        raise FileNotFoundError("no dpkg-query")

    monkeypatch.setattr(pf.subprocess, "run", _fake_run)
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_kernel_dev_libs(ctx)
    # Conservative: pass rather than spurious fail when we can't tell.
    assert r.severity == pf.Severity.PASS


def test_kernel_dev_libs_fails_when_libdw_missing(monkeypatch):
    """Modern GENDWARFKSYMS builds need libdw-dev for <dwarf.h>."""

    def _fake_run(*args, **kwargs):
        class R:
            returncode = 0
            stdout = (
                "libssl-dev install ok installed\n"
                "libelf-dev install ok installed\n"
                "libncurses-dev install ok installed\n"
            )

        return R()

    monkeypatch.setattr(pf.subprocess, "run", _fake_run)
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_kernel_dev_libs(ctx)
    assert r.severity == pf.Severity.FAIL
    assert "libdw-dev" in r.message
    assert "libdw-dev" in (r.fix_hint or "")


def test_distro_build_packages_warns_for_missing_package_deps(monkeypatch):
    def _fake_run(*args, **kwargs):
        class R:
            returncode = 0
            stdout = (
                "build-essential install ok installed\n"
                "flex install ok installed\n"
                "bison install ok installed\n"
                "bc install ok installed\n"
                "libssl-dev install ok installed\n"
                "libelf-dev install ok installed\n"
                "libncurses-dev install ok installed\n"
                "dwarves install ok installed\n"
                "zstd install ok installed\n"
                "kmod install ok installed\n"
                "cpio install ok installed\n"
                "rsync install ok installed\n"
                "clang install ok installed\n"
                "lld install ok installed\n"
            )

        return R()

    monkeypatch.setattr(pf.subprocess, "run", _fake_run)
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_distro_build_packages(ctx)
    assert r.severity == pf.Severity.WARN
    assert "libdw-dev" in r.message
    assert "debhelper" in r.message
    assert "llvm" in r.message


# ── snapshot-aware checks ──────────────────────────────────────────────────


def test_snapshot_running_config_pass_with_snapshot(intel_laptop):
    ctx = _ctx_for_family(Family.DEBIAN, snapshot=intel_laptop)
    r = pf.check_snapshot_running_config(ctx)
    assert r.severity == pf.Severity.PASS


def test_snapshot_running_config_skip_without_snapshot():
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_snapshot_running_config(ctx)
    assert r.severity == pf.Severity.SKIP


def test_snapshot_dkms_warn_when_present(amd_desktop):
    ctx = _ctx_for_family(Family.DEBIAN, snapshot=amd_desktop)
    r = pf.check_snapshot_dkms_clean(ctx)
    assert r.severity == pf.Severity.WARN
    assert "nvidia" in r.message


def test_snapshot_dkms_pass_when_absent(intel_laptop):
    ctx = _ctx_for_family(Family.DEBIAN, snapshot=intel_laptop)
    r = pf.check_snapshot_dkms_clean(ctx)
    assert r.severity == pf.Severity.PASS


# ── install readiness checks ────────────────────────────────────────────────


def test_install_packages_warn_when_missing(tmp_path):
    ctx = _ctx_for_family(
        Family.DEBIAN, snapshot=SimpleNamespace(snapshot_dir=tmp_path)
    )
    r = pf.check_install_packages_present(ctx)
    assert r.severity == pf.Severity.WARN
    assert "no installable" in r.message


def test_install_packages_pass_when_package_exists(tmp_path):
    pkg = tmp_path / "linux-image-6.13.5_amd64.deb"
    pkg.write_text("")
    ctx = _ctx_for_family(
        Family.DEBIAN, snapshot=SimpleNamespace(snapshot_dir=tmp_path)
    )
    r = pf.check_install_packages_present(ctx)
    assert r.severity == pf.Severity.PASS


def test_install_packages_pass_when_explicit_packages_provided(tmp_path):
    pkg = tmp_path / "linux-image-6.13.5_amd64.deb"
    ctx = _ctx_for_family(
        Family.DEBIAN, snapshot=SimpleNamespace(snapshot_dir=tmp_path)
    )
    ctx.package_paths = (pkg,)
    r = pf.check_install_packages_present(ctx)
    assert r.severity == pf.Severity.PASS
    assert "explicit" in r.message


def test_boot_test_record_warns_when_missing(tmp_path):
    ctx = _ctx_for_family(
        Family.DEBIAN, snapshot=SimpleNamespace(snapshot_dir=tmp_path)
    )
    r = pf.check_boot_test_record(ctx)
    assert r.severity == pf.Severity.WARN
    assert "no boot-test" in r.message


def test_boot_test_record_warns_for_qemu_kernel_only(tmp_path):
    (tmp_path / "boot-test.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "verdict_ok": True,
                "kernel_release": "7.1.0-rc3",
                "method": "qemu",
            }
        )
    )
    ctx = _ctx_for_family(
        Family.DEBIAN, snapshot=SimpleNamespace(snapshot_dir=tmp_path)
    )
    r = pf.check_boot_test_record(ctx)
    assert r.severity == pf.Severity.WARN
    assert "kernel-only" in r.message


def test_boot_test_record_fails_for_failed_record(tmp_path):
    (tmp_path / "boot-test.json").write_text(
        json.dumps({"schema": 1, "verdict_ok": False, "verdict_reason": "panic"})
    )
    ctx = _ctx_for_family(
        Family.DEBIAN, snapshot=SimpleNamespace(snapshot_dir=tmp_path)
    )
    r = pf.check_boot_test_record(ctx)
    assert r.severity == pf.Severity.FAIL
    assert "panic" in r.message


# ── boot-test runtime checks ────────────────────────────────────────────────


def test_boot_test_runtime_passes_with_qemu(monkeypatch):
    def _which(c):
        return "/usr/bin/qemu-system-x86_64" if c == "qemu-system-x86_64" else None

    monkeypatch.setattr(pf.shutil, "which", _which)
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_boot_test_runtime_available(ctx)
    assert r.severity == pf.Severity.PASS
    assert "qemu-system-x86_64" in r.message


def test_boot_test_runtime_passes_with_virtme(monkeypatch):
    def _which(c):
        return "/usr/bin/virtme-ng" if c == "virtme-ng" else None

    monkeypatch.setattr(pf.shutil, "which", _which)
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_boot_test_runtime_available(ctx)
    assert r.severity == pf.Severity.PASS
    assert "virtme" in r.message


def test_boot_test_runtime_fails_with_neither_runtime(monkeypatch):
    monkeypatch.setattr(pf.shutil, "which", lambda c: None)
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_boot_test_runtime_available(ctx)
    assert r.severity == pf.Severity.FAIL
    assert "qemu-system-x86_64" in r.message
    assert "qemu-system-x86" in (r.fix_hint or "")
    assert "virtme-ng" in (r.fix_hint or "")


def test_virtme_config_support_skips_without_kernel_source():
    ctx = _ctx_for_family(Family.DEBIAN)
    r = pf.check_virtme_config_support(ctx)
    assert r.severity == pf.Severity.SKIP


def test_virtme_config_support_warns_when_transport_missing(tmp_path, monkeypatch):
    (tmp_path / ".config").write_text(
        "# CONFIG_NET_9P is not set\n# CONFIG_VIRTIO_FS is not set\n"
    )
    monkeypatch.setattr(
        pf.shutil,
        "which",
        lambda c: "/usr/bin/qemu-system-x86_64" if c == "qemu-system-x86_64" else None,
    )
    ctx = _ctx_for_family(Family.DEBIAN)
    ctx.kernel_source = tmp_path
    r = pf.check_virtme_config_support(ctx)
    assert r.severity == pf.Severity.WARN
    assert "virtme" in r.message


# ── tag dispatch ────────────────────────────────────────────────────────────


def test_run_checks_filters_by_tags(monkeypatch):
    """--for=propose should run propose-tagged checks but skip build-only ones."""
    monkeypatch.setattr(pf.shutil, "which", lambda c: f"/usr/bin/{c}")
    info = parse_os_release("ID=ubuntu\nID_LIKE=debian\n")
    run = pf.run_checks(tags={"always", "propose"}, distro=info)
    names = {r.name for r in run.results}
    assert "distro_recognized" in names  # always
    assert "build_tools" not in names  # build-only
    assert "free_disk_space" not in names  # build-only


def test_run_checks_includes_snapshot_checks_when_provided(intel_laptop, monkeypatch):
    monkeypatch.setattr(pf.shutil, "which", lambda c: f"/usr/bin/{c}")
    info = parse_os_release("ID=ubuntu\nID_LIKE=debian\n")
    run = pf.run_checks(tags={"always", "propose"}, distro=info, snapshot=intel_laptop)
    names = {r.name for r in run.results}
    assert "snapshot_running_config" in names


def test_run_checks_skips_snapshot_checks_with_no_snapshot(monkeypatch):
    monkeypatch.setattr(pf.shutil, "which", lambda c: f"/usr/bin/{c}")
    info = parse_os_release("ID=ubuntu\nID_LIKE=debian\n")
    run = pf.run_checks(tags={"always", "propose"}, distro=info, snapshot=None)
    snapshot_results = [r for r in run.results if r.name.startswith("snapshot_")]
    # Snapshot checks appear in the result with SKIP severity, not omitted.
    assert all(r.severity == pf.Severity.SKIP for r in snapshot_results)


def test_run_checks_has_failures_property(monkeypatch):
    """If any FAIL is present, has_failures is True."""
    monkeypatch.setattr(
        pf.shutil, "which", lambda c: None if c == "flex" else f"/usr/bin/{c}"
    )
    info = parse_os_release("ID=ubuntu\nID_LIKE=debian\n")
    run = pf.run_checks(tags={"build"}, distro=info)
    assert run.has_failures
