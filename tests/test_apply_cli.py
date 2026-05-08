"""Integration tests for `autokernel apply`."""

from __future__ import annotations

import shutil
from pathlib import Path

from typer.testing import CliRunner

from autokernel.cli import app

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


def _seed(tmp_path: Path, kfrag_text: str) -> Path:
    """Copy the intel_laptop fixture into tmp_path and drop a kfrag in."""
    snap = tmp_path / "snap"
    shutil.copytree(FIXTURES / "intel_laptop", snap)
    (snap / "auto.kfrag").write_text(kfrag_text)
    return snap


def test_apply_disables_safe_symbol(tmp_path: Path):
    snap = _seed(
        tmp_path,
        "# autokernel-kfrag schema=1\n"
        "# CONFIG_104_QUAD_8 is not set\n"
        "# CONFIG_DRM_NOUVEAU is not set\n",
    )
    result = runner.invoke(app, ["apply", str(snap)])
    assert result.exit_code == 0, result.output

    final = (snap / "final.config").read_text()
    assert "# CONFIG_104_QUAD_8 is not set" in final
    assert "# CONFIG_DRM_NOUVEAU is not set" in final
    # untouched symbol preserved
    assert "CONFIG_DRM_I915=m" in final


def test_apply_blocks_load_bearing_disable(tmp_path: Path):
    """Disabling CONFIG_DRM_I915 (the active GPU driver) must be refused."""
    snap = _seed(
        tmp_path,
        "# autokernel-kfrag schema=1\n# CONFIG_DRM_I915 is not set\n",
    )
    result = runner.invoke(app, ["apply", str(snap)])
    # Validation should have caught it
    assert result.exit_code == 4, result.output
    assert "load-bearing" in result.output.lower()


def test_apply_no_validate_overrides(tmp_path: Path):
    """--no-validate writes the file even with load-bearing violations."""
    snap = _seed(
        tmp_path,
        "# autokernel-kfrag schema=1\n# CONFIG_DRM_I915 is not set\n",
    )
    result = runner.invoke(app, ["apply", str(snap), "--no-validate"])
    assert result.exit_code == 0, result.output
    final = (snap / "final.config").read_text()
    assert "# CONFIG_DRM_I915 is not set" in final


def test_apply_missing_kfrag_exits_2(tmp_path: Path):
    snap = tmp_path / "s"
    shutil.copytree(FIXTURES / "intel_laptop", snap)
    # No auto.kfrag — apply must fail clearly.
    result = runner.invoke(app, ["apply", str(snap)])
    assert result.exit_code == 2
    assert "kfrag not found" in result.output.lower()


def test_apply_demote_y_to_m(tmp_path: Path):
    snap = _seed(
        tmp_path,
        "# autokernel-kfrag schema=1\n"
        "CONFIG_DRM_AMDGPU=m\n",  # already m in fixture; should be a no-op
        # add a real demotion target
    )
    # Override the kfrag with a real y→m demote
    (snap / "auto.kfrag").write_text(
        "# autokernel-kfrag schema=1\nCONFIG_DRM_I915_GVT=m\n",  # base has =y
    )
    result = runner.invoke(app, ["apply", str(snap)])
    assert result.exit_code == 0, result.output

    final = (snap / "final.config").read_text()
    assert "CONFIG_DRM_I915_GVT=m" in final
    assert "CONFIG_DRM_I915_GVT=y" not in final


def test_apply_round_trip_idempotent(tmp_path: Path):
    """Applying the same kfrag twice produces the same output."""
    snap = _seed(
        tmp_path,
        "# autokernel-kfrag schema=1\n"
        "# CONFIG_104_QUAD_8 is not set\n"
        "# CONFIG_60XX_WDT is not set\n",
    )
    r1 = runner.invoke(app, ["apply", str(snap)])
    assert r1.exit_code == 0
    final1 = (snap / "final.config").read_text()

    # Re-run; final.config should be identical.
    r2 = runner.invoke(app, ["apply", str(snap)])
    assert r2.exit_code == 0
    final2 = (snap / "final.config").read_text()
    assert final1 == final2
