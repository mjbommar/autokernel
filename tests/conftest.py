"""Test fixtures shared across the autokernel test suite.

Each fixture in ``tests/fixtures/<name>/`` is a directory of files mirroring
what ``scripts/collect.sh`` produces. ``snapshot.load`` parses these into a
:class:`autokernel.models.Snapshot` exactly as it would for a real host.

The synthetic fixtures avoid the original test-suite trap of asserting against
the host the tests happen to run on.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from autokernel import snapshot as snap_mod
from autokernel.models import Snapshot

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(name: str, tmp_path_factory: pytest.TempPathFactory) -> Snapshot:
    # Load from a per-session copy: resolve() memoizes modinfo lookups into
    # <snapshot>/loadable_modinfo_cache, which would otherwise rewrite the
    # tracked fixture with whatever modules the host happens to have.
    dst = tmp_path_factory.mktemp("fixture") / name
    shutil.copytree(FIXTURES_DIR / name, dst)
    return snap_mod.load(dst)


@pytest.fixture(scope="session")
def intel_laptop(tmp_path_factory: pytest.TempPathFactory) -> Snapshot:
    """EFI + Secure Boot, btrfs-on-LUKS root, Intel iGPU only, iwlwifi."""
    return _load("intel_laptop", tmp_path_factory)


@pytest.fixture(scope="session")
def amd_desktop(tmp_path_factory: pytest.TempPathFactory) -> Snapshot:
    """EFI no-SB, ext4 root no LUKS, NVIDIA dGPU + AMD CPU, r8169, DKMS nvidia."""
    return _load("amd_desktop", tmp_path_factory)
