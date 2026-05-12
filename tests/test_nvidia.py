from __future__ import annotations

from pathlib import Path

from autokernel.distro import parse_os_release
from autokernel.nvidia import (
    NvidiaMode,
    NvidiaPackage,
    kernel_release_from_packages,
    plan_nvidia_support,
)


def _ubuntu():
    return parse_os_release("ID=ubuntu\nID_LIKE=debian\n")


def test_kernel_release_from_debian_image_package(tmp_path: Path):
    pkg = tmp_path / "linux-image-7.0.0-autokernel-202605091447_7.0.0-1_amd64.deb"
    pkg.write_text("")
    assert kernel_release_from_packages([pkg]) == "7.0.0-autokernel-202605091447"


def test_no_plan_without_nvidia_gpu(intel_laptop, tmp_path: Path):
    pkg = tmp_path / "linux-image-7.0.0-autokernel_7.0.0-1_amd64.deb"
    pkg.write_text("")
    plan = plan_nvidia_support(
        snapshot=intel_laptop,
        distro=_ubuntu(),
        package_paths=[pkg],
        installed_packages=[NvidiaPackage("nvidia-driver-580")],
    )
    assert plan is None


def test_auto_preserves_proprietary_driver_flavor(amd_desktop, tmp_path: Path):
    pkg = tmp_path / "linux-image-7.0.0-autokernel_7.0.0-1_amd64.deb"
    pkg.write_text("")
    plan = plan_nvidia_support(
        snapshot=amd_desktop,
        distro=_ubuntu(),
        package_paths=[pkg],
        installed_packages=[NvidiaPackage("nvidia-driver-580")],
    )
    assert plan is not None
    assert plan.branch == "580"
    assert plan.flavor == "proprietary"
    assert plan.package_name == "nvidia-driver-580"


def test_auto_preserves_open_driver_flavor(amd_desktop, tmp_path: Path):
    pkg = tmp_path / "linux-image-7.0.0-autokernel_7.0.0-1_amd64.deb"
    pkg.write_text("")
    plan = plan_nvidia_support(
        snapshot=amd_desktop,
        distro=_ubuntu(),
        package_paths=[pkg],
        installed_packages=[NvidiaPackage("nvidia-driver-580-open")],
    )
    assert plan is not None
    assert plan.flavor == "open"
    assert plan.package_name == "nvidia-driver-580-open"


def test_auto_prefers_driver_metapackage_over_old_open_module_package(
    amd_desktop, tmp_path: Path
):
    pkg = tmp_path / "linux-image-7.0.0-autokernel_7.0.0-1_amd64.deb"
    pkg.write_text("")
    plan = plan_nvidia_support(
        snapshot=amd_desktop,
        distro=_ubuntu(),
        package_paths=[pkg],
        installed_packages=[
            NvidiaPackage("nvidia-driver-580"),
            NvidiaPackage("linux-modules-nvidia-580-open-7.0.0-10-generic"),
        ],
    )
    assert plan is not None
    assert plan.flavor == "proprietary"
    assert plan.package_name == "nvidia-driver-580"


def test_user_can_force_open_flavor(amd_desktop, tmp_path: Path):
    pkg = tmp_path / "linux-image-7.0.0-autokernel_7.0.0-1_amd64.deb"
    pkg.write_text("")
    plan = plan_nvidia_support(
        snapshot=amd_desktop,
        distro=_ubuntu(),
        package_paths=[pkg],
        mode=NvidiaMode.OPEN,
        installed_packages=[NvidiaPackage("nvidia-driver-580")],
    )
    assert plan is not None
    assert plan.package_name == "nvidia-driver-580-open"


def test_dkms_status_can_supply_branch(amd_desktop, tmp_path: Path):
    pkg = tmp_path / "linux-image-7.0.0-autokernel_7.0.0-1_amd64.deb"
    pkg.write_text("")
    plan = plan_nvidia_support(
        snapshot=amd_desktop,
        distro=_ubuntu(),
        package_paths=[pkg],
        installed_packages=[],
    )
    assert plan is not None
    assert plan.branch == "550"
    assert plan.package_name == "nvidia-driver-550"
