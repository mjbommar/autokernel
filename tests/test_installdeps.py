"""Tests for the install-deps planner. Subprocess is mocked throughout."""

from __future__ import annotations

from typing import Any

import pytest

from autokernel import installdeps as id_mod
from autokernel.distro import DistroInfo, Family, parse_os_release, spec_for
from autokernel.installdeps import (
    Target,
    plan,
)


def _info(family: Family) -> DistroInfo:
    return parse_os_release(
        {
            Family.DEBIAN: "ID=ubuntu\nID_LIKE=debian\n",
            Family.FEDORA: "ID=fedora\n",
            Family.ARCH: "ID=arch\n",
            Family.SUSE: "ID=opensuse-tumbleweed\n",
            Family.UNKNOWN: "ID=mystery\n",
        }[family]
    )


# ── per-family install command construction ───────────────────────────────


def test_debian_plan_uses_apt_install_y(monkeypatch):
    info = _info(Family.DEBIAN)
    # Probe says nothing's installed
    monkeypatch.setattr(
        id_mod,
        "_query_installed",
        lambda fam, pkgs: (pkgs, []),
    )
    p = plan(distro=info, spec=spec_for(info), target=Target.BUILD)
    assert p.install_cmd[0:3] == ["apt", "install", "-y"]
    assert "build-essential" in p.missing
    assert "flex" in p.missing


def test_fedora_plan_uses_dnf_install_y(monkeypatch):
    info = _info(Family.FEDORA)
    monkeypatch.setattr(id_mod, "_query_installed", lambda fam, pkgs: (pkgs, []))
    p = plan(distro=info, spec=spec_for(info), target=Target.BUILD)
    assert p.install_cmd[0:3] == ["dnf", "install", "-y"]
    assert "openssl-devel" in p.missing


def test_arch_plan_uses_pacman_S(monkeypatch):
    info = _info(Family.ARCH)
    monkeypatch.setattr(id_mod, "_query_installed", lambda fam, pkgs: (pkgs, []))
    p = plan(distro=info, spec=spec_for(info), target=Target.BUILD)
    assert p.install_cmd[0] == "pacman"
    assert "base-devel" in p.missing


def test_unknown_family_returns_rejected(monkeypatch):
    info = _info(Family.UNKNOWN)
    p = plan(distro=info, spec=spec_for(info), target=Target.BUILD)
    assert not p.is_valid
    assert p.rejected_reason is not None
    assert "UNKNOWN" in p.rejected_reason


# ── target-specific composition ────────────────────────────────────────────


def test_target_build_includes_only_build_deps(monkeypatch):
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda fam, pkgs: (pkgs, []))
    p = plan(distro=info, spec=spec_for(info), target=Target.BUILD)
    assert "build-essential" in p.requested
    # Boot-test extras must NOT be in build target
    assert "qemu-system-x86" not in p.requested


def test_target_boot_test_includes_qemu(monkeypatch):
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda fam, pkgs: (pkgs, []))
    monkeypatch.setattr(id_mod, "_virtme_installed", lambda: False)
    p = plan(distro=info, spec=spec_for(info), target=Target.BOOT_TEST)
    assert "qemu-system-x86" in p.requested
    # Build-only deps shouldn't appear in boot-test target
    assert "build-essential" not in p.requested
    # virtme-ng surfaces as an optional python package
    assert "virtme-ng" in p.optional_python_pkgs


def test_target_all_unions(monkeypatch):
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda fam, pkgs: (pkgs, []))
    monkeypatch.setattr(id_mod, "_virtme_installed", lambda: False)
    p = plan(distro=info, spec=spec_for(info), target=Target.ALL)
    assert "build-essential" in p.requested
    assert "qemu-system-x86" in p.requested
    assert "grub2-common" in p.requested  # install-target extras


def test_target_world_includes_rebuild_toolchain(monkeypatch):
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda fam, pkgs: (pkgs, []))
    p = plan(distro=info, spec=spec_for(info), target=Target.WORLD)
    for pkg in ("sbuild", "mmdebstrap", "blhc", "autopkgtest", "devscripts"):
        assert pkg in p.requested
    # world is its own pipeline: no kernel build deps, no qemu, no virtme
    assert "build-essential" not in p.requested
    assert "qemu-system-x86" not in p.requested
    assert p.optional_python_pkgs == []
    # ccache rides along as a recommended package
    assert "ccache" in p.requested


def test_target_world_rejected_on_non_debian(monkeypatch):
    info = _info(Family.FEDORA)
    monkeypatch.setattr(id_mod, "_query_installed", lambda fam, pkgs: (pkgs, []))
    p = plan(distro=info, spec=spec_for(info), target=Target.WORLD)
    assert not p.is_valid
    assert p.rejected_reason is not None
    assert "Debian" in p.rejected_reason


def test_target_all_excludes_world_toolchain(monkeypatch):
    """`all` is the kernel-pipeline trio; the world toolchain must stay
    opt-in via --for world."""
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda fam, pkgs: (pkgs, []))
    monkeypatch.setattr(id_mod, "_virtme_installed", lambda: False)
    p = plan(distro=info, spec=spec_for(info), target=Target.ALL)
    assert "sbuild" not in p.requested
    assert "mmdebstrap" not in p.requested


def test_recommended_includes_ccache_by_default(monkeypatch):
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda fam, pkgs: (pkgs, []))
    p = plan(distro=info, spec=spec_for(info), target=Target.BUILD, recommended=True)
    assert "ccache" in p.requested


def test_no_recommended_excludes_ccache(monkeypatch):
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda fam, pkgs: (pkgs, []))
    p = plan(distro=info, spec=spec_for(info), target=Target.BUILD, recommended=False)
    assert "ccache" not in p.requested


def test_no_virtme_omits_optional(monkeypatch):
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda fam, pkgs: (pkgs, []))
    p = plan(
        distro=info,
        spec=spec_for(info),
        target=Target.BOOT_TEST,
        include_virtme=False,
    )
    assert p.optional_python_pkgs == []


def test_virtme_already_installed_omits_optional(monkeypatch):
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda fam, pkgs: (pkgs, []))
    monkeypatch.setattr(id_mod, "_virtme_installed", lambda: True)
    p = plan(distro=info, spec=spec_for(info), target=Target.BOOT_TEST)
    assert p.optional_python_pkgs == []


# ── package-installed detection ────────────────────────────────────────────


def test_query_installed_debian_dpkg(monkeypatch):
    """dpkg-query returns 'name install ok installed' for installed packages."""

    def _fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = (
                "flex install ok installed\n"
                "bison install ok installed\n"
                "libssl-dev unknown ok not-installed\n"
            )

        return R()

    monkeypatch.setattr(id_mod.subprocess, "run", _fake_run)
    missing, installed = id_mod._query_installed(
        Family.DEBIAN, ["flex", "bison", "libssl-dev"]
    )
    assert installed == ["flex", "bison"]
    assert missing == ["libssl-dev"]


def test_query_installed_arch_pacman(monkeypatch):
    def _fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = "base-devel\nflex\nopenssl\n"

        return R()

    monkeypatch.setattr(id_mod.subprocess, "run", _fake_run)
    missing, installed = id_mod._query_installed(
        Family.ARCH, ["base-devel", "flex", "ncurses"]
    )
    assert "base-devel" in installed
    assert "flex" in installed
    assert "ncurses" in missing


def test_query_installed_falls_back_to_all_missing_on_query_failure(monkeypatch):
    """If dpkg-query is unavailable (or fails), behave conservatively:
    treat everything as missing. apt itself is no-op when a package is
    already installed, so this is safe."""

    def _fake_run(cmd, **kwargs):
        raise FileNotFoundError("no dpkg-query")

    monkeypatch.setattr(id_mod.subprocess, "run", _fake_run)
    missing, installed = id_mod._query_installed(Family.DEBIAN, ["flex", "bison"])
    assert missing == ["flex", "bison"]
    assert installed == []


# ── plan invariants ───────────────────────────────────────────────────────


def test_full_argv_includes_sudo_when_not_root(monkeypatch):
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda f, p: (p, []))
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    p = plan(distro=info, spec=spec_for(info), target=Target.BUILD)
    assert p.full_argv[0] == "sudo"
    assert "apt" in p.full_argv


def test_full_argv_omits_sudo_when_root(monkeypatch):
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda f, p: (p, []))
    monkeypatch.setattr("os.geteuid", lambda: 0)
    p = plan(distro=info, spec=spec_for(info), target=Target.BUILD)
    assert p.full_argv[0] == "apt"


def test_full_argv_empty_when_nothing_missing(monkeypatch):
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda f, p: ([], list(p)))
    p = plan(distro=info, spec=spec_for(info), target=Target.BUILD)
    assert p.full_argv == []


def test_needs_anything_false_when_nothing_missing(monkeypatch):
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda f, p: ([], list(p)))
    monkeypatch.setattr(id_mod, "_virtme_installed", lambda: True)
    p = plan(distro=info, spec=spec_for(info), target=Target.ALL)
    assert not p.needs_anything


def test_no_duplicate_packages_across_targets(monkeypatch):
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda f, p: (p, []))
    monkeypatch.setattr(id_mod, "_virtme_installed", lambda: False)
    p = plan(distro=info, spec=spec_for(info), target=Target.ALL)
    assert len(p.requested) == len(set(p.requested))


# ── execute() with mocked subprocess ──────────────────────────────────────


@pytest.fixture
def captured_runs(monkeypatch):
    calls: list[dict[str, Any]] = []

    class _R:
        def __init__(self, rc=0):
            self.returncode = rc

    def _fake_run(argv, **kwargs):
        # Accommodate both "logged to file" (stdout=fileobj) and
        # "inherit stdio" (no stdout kwarg) execution paths.
        f = kwargs.get("stdout")
        if hasattr(f, "write"):
            f.write(b"")
        calls.append({"argv": list(argv), **kwargs})
        return _R(0)

    monkeypatch.setattr(id_mod.subprocess, "run", _fake_run)
    return calls


def test_execute_runs_install_command(monkeypatch, captured_runs, tmp_path):
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda f, p: (p[:2], p[2:]))
    monkeypatch.setattr(id_mod, "_virtme_installed", lambda: True)
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    p = plan(distro=info, spec=spec_for(info), target=Target.BUILD)
    log_dir = tmp_path / "logs"
    result = id_mod.execute(p, log_dir=log_dir)
    assert result.ok
    assert len(captured_runs) >= 1
    # First call is sudo apt install -y …
    assert captured_runs[0]["argv"][0] == "sudo"
    assert "apt" in captured_runs[0]["argv"]


def test_execute_uses_uv_tool_install_for_optional_pkgs(
    monkeypatch, captured_runs, tmp_path
):
    """Optional Python tools install via `uv tool install`, not pip --user."""
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda f, p: ([], list(p)))
    monkeypatch.setattr(id_mod, "_virtme_installed", lambda: False)
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    p = plan(distro=info, spec=spec_for(info), target=Target.BOOT_TEST)
    assert "virtme-ng" in p.optional_python_pkgs
    result = id_mod.execute(p, log_dir=tmp_path / "logs")
    assert result.ok
    # The optional install should be `uv tool install virtme-ng` (no sudo, no pip).
    virtme_calls = [c for c in captured_runs if "virtme-ng" in c["argv"]]
    assert len(virtme_calls) == 1
    assert virtme_calls[0]["argv"][:3] == ["uv", "tool", "install"]
    assert virtme_calls[0]["argv"][3] == "virtme-ng"
    # No sudo on a uv-tool install.
    assert "sudo" not in virtme_calls[0]["argv"]


def test_execute_skips_when_nothing_missing(monkeypatch, captured_runs, tmp_path):
    info = _info(Family.DEBIAN)
    monkeypatch.setattr(id_mod, "_query_installed", lambda f, p: ([], list(p)))
    monkeypatch.setattr(id_mod, "_virtme_installed", lambda: True)
    p = plan(distro=info, spec=spec_for(info), target=Target.BUILD)
    result = id_mod.execute(p, log_dir=tmp_path / "logs")
    assert result.ok  # vacuously
    assert captured_runs == []
    assert result.runs == []


def test_execute_refuses_invalid_plan(captured_runs):
    info = _info(Family.UNKNOWN)
    p = plan(distro=info, spec=spec_for(info), target=Target.BUILD)
    with pytest.raises(RuntimeError):
        id_mod.execute(p)
    assert captured_runs == []
