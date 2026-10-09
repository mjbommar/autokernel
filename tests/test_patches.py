"""Patch-queue tests against a real (tiny) git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autokernel import patches as pm

GIT = [
    "git",
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@t",
    "-c",
    "init.defaultBranch=main",
]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [*GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _kernel_repo(tmp_path: Path, extraversion: str = "-rc6") -> Path:
    repo = tmp_path / "linux"
    repo.mkdir()
    (repo / "Makefile").write_text(
        f"VERSION = 7\nPATCHLEVEL = 3\nSUBLEVEL = 0\nEXTRAVERSION = {extraversion}\n"
    )
    (repo / "domain.h").write_text(
        "struct dom {\n\tint usage;\n\tint num_layers;\n};\n"
    )
    subprocess.run([*GIT, "init", "-q", str(repo)], check=True)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "Linux 7.3-rc6")
    return repo


def _make_patch(repo: Path, out: Path, old: str, new: str, subject: str) -> Path:
    """Commit a change, export it with format-patch, then reset it away."""
    f = repo / "domain.h"
    f.write_text(f.read_text().replace(old, new))
    _git(repo, "commit", "-q", "-am", subject)
    out.mkdir(parents=True, exist_ok=True)
    name = _git(repo, "format-patch", "-1", "-o", str(out)).strip()
    _git(repo, "reset", "-q", "--hard", "HEAD~1")
    return Path(name)


def _make_set(
    root: Path, name: str, patch_files: list[Path], meta_extra: str = ""
) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for p in patch_files:
        if p.parent != d:
            p.rename(d / p.name)
    (d / "series").write_text("".join(f"{p.name}\n" for p in patch_files))
    (d / "meta.toml").write_text(
        f'description = "test"\nkind = "upstream-pending"\n{meta_extra}'
    )
    return d


# ── versions ────────────────────────────────────────────────────────────────


def test_version_ordering():
    v = pm.parse_version
    assert v("7.3-rc5") < v("7.3-rc6") < v("7.3") < v("7.3.1") < v("7.4-rc1")
    assert v("v7.3.0") == v("7.3")


def test_source_version_reads_makefile(tmp_path):
    repo = _kernel_repo(tmp_path)
    assert pm.source_version(repo) == pm.parse_version("7.3-rc6")


def test_bad_version_rejected():
    with pytest.raises(pm.PatchError, match="unparseable"):
        pm.parse_version("seven")


# ── loading ─────────────────────────────────────────────────────────────────


def test_load_set_validates_kind_and_series(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "series").write_text("missing.patch\n")
    (d / "meta.toml").write_text('kind = "upstream-pending"\n')
    with pytest.raises(pm.PatchError, match="not found"):
        pm.load_set(d)
    (d / "meta.toml").write_text('kind = "whatever"\n')
    with pytest.raises(pm.PatchError, match="kind="):
        pm.load_set(d)


def test_meta_must_reference_series_entries(tmp_path):
    repo = _kernel_repo(tmp_path)
    p = _make_patch(repo, tmp_path / "out", "int usage;", "long usage;", "widen usage")
    d = _make_set(
        tmp_path / "patches", "s", [p], '[patches."other.patch"]\nstatus = "posted"\n'
    )
    with pytest.raises(pm.PatchError, match="not in series"):
        pm.load_set(d)


def test_version_range(tmp_path):
    repo = _kernel_repo(tmp_path)
    p = _make_patch(repo, tmp_path / "out", "int usage;", "long usage;", "widen usage")
    _make_set(
        tmp_path / "patches", "s", [p], 'min_version = "7.3-rc1"\nmax_version = "7.3"\n'
    )
    ps = pm.load_set(tmp_path / "patches" / "s")
    assert ps.applies_to(pm.parse_version("7.3-rc6"))
    assert not ps.applies_to(pm.parse_version("7.3"))
    assert ps.version_range == "[7.3-rc1, 7.3)"


def test_resolve_unknown_set(tmp_path):
    (tmp_path / "patches").mkdir()
    with pytest.raises(pm.PatchError, match="unknown patch set"):
        pm.resolve(["nope"], tmp_path / "patches")


# ── check / apply ───────────────────────────────────────────────────────────


def test_check_is_non_destructive(tmp_path):
    repo = _kernel_repo(tmp_path)
    p = _make_patch(repo, tmp_path / "out", "int usage;", "long usage;", "widen usage")
    _make_set(tmp_path / "patches", "s", [p])
    head = _git(repo, "rev-parse", "HEAD")
    out = pm.check(pm.resolve(["s"], tmp_path / "patches"), repo)
    assert [o.result for o in out] == ["would-apply"]
    assert _git(repo, "rev-parse", "HEAD") == head
    assert "long usage" not in (repo / "domain.h").read_text()


def test_apply_creates_branch_and_provenance(tmp_path):
    repo = _kernel_repo(tmp_path)
    p = _make_patch(repo, tmp_path / "out", "int usage;", "long usage;", "widen usage")
    _make_set(
        tmp_path / "patches",
        "s",
        [p],
        f'[patches."{p.name}"]\nstatus = "posted"\nlink = "https://lore.example/x"\n',
    )
    base = _git(repo, "rev-parse", "HEAD").strip()
    res = pm.apply(
        pm.resolve(["s"], tmp_path / "patches"), repo, branch="autokernel/test"
    )
    assert res.ok and res.base_commit == base and res.head_commit != base
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "autokernel/test"
    assert "long usage" in (repo / "domain.h").read_text()
    assert res.provenance()["kernel_version"] == "7.3-rc6"
    (o,) = [x.as_dict() for x in res.outcomes]
    assert o["result"] == "applied" and o["subject"] == "widen usage"
    assert o["status"] == "posted" and len(o["sha256"]) == 64


def test_apply_skips_patch_already_present(tmp_path):
    repo = _kernel_repo(tmp_path)
    p = _make_patch(repo, tmp_path / "out", "int usage;", "long usage;", "widen usage")
    _make_set(tmp_path / "patches", "s", [p])
    # Upstream "merged" it: tree already contains the change.
    (repo / "domain.h").write_text(
        (repo / "domain.h").read_text().replace("int usage;", "long usage;")
    )
    _git(repo, "commit", "-q", "-am", "upstream merge")
    res = pm.apply(pm.resolve(["s"], tmp_path / "patches"), repo)
    assert [o.result for o in res.outcomes] == ["present"]
    assert res.ok


def test_apply_conflict_restores_tree(tmp_path):
    repo = _kernel_repo(tmp_path)
    p = _make_patch(repo, tmp_path / "out", "int usage;", "long usage;", "widen usage")
    _make_set(tmp_path / "patches", "s", [p])
    (repo / "domain.h").write_text("struct dom {\n\tchar usage;\n};\n")
    _git(repo, "commit", "-q", "-am", "diverge")
    base = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(pm.PatchError, match="does not apply") as ei:
        pm.apply(pm.resolve(["s"], tmp_path / "patches"), repo, branch="autokernel/x")
    assert [o.result for o in ei.value.outcomes] == ["conflict"]
    assert _git(repo, "rev-parse", "HEAD") == base
    assert "autokernel/x" not in _git(repo, "branch")
    assert not pm.tracked_changes(repo)


def test_apply_refuses_dirty_tree(tmp_path):
    repo = _kernel_repo(tmp_path)
    p = _make_patch(repo, tmp_path / "out", "int usage;", "long usage;", "widen usage")
    _make_set(tmp_path / "patches", "s", [p])
    (repo / "domain.h").write_text("dirty\n")
    with pytest.raises(pm.PatchError, match="modified tracked files"):
        pm.apply(pm.resolve(["s"], tmp_path / "patches"), repo)


def test_apply_refuses_out_of_range_version(tmp_path):
    repo = _kernel_repo(tmp_path, extraversion="")  # 7.3 final
    p = _make_patch(repo, tmp_path / "out", "int usage;", "long usage;", "widen usage")
    _make_set(tmp_path / "patches", "s", [p], 'max_version = "7.3"\n')
    sets = pm.resolve(["s"], tmp_path / "patches")
    with pytest.raises(pm.PatchError, match="not declared for kernel 7.3"):
        pm.apply(sets, repo)
    assert pm.apply(sets, repo, allow_version_mismatch=True).ok


def test_non_git_tree_rejected(tmp_path):
    d = tmp_path / "tarball"
    d.mkdir()
    (d / "Makefile").write_text("VERSION = 7\nPATCHLEVEL = 3\n")
    with pytest.raises(pm.PatchError, match="not a git work tree"):
        pm.require_git_tree(d)


def test_repo_patch_sets_are_valid():
    """Every set shipped in <repo>/patches must load."""
    for ps in pm.discover():
        assert ps.patches


# ── per-set Kconfig ─────────────────────────────────────────────────────────


def test_set_config_parsed_and_merged(tmp_path):
    repo = _kernel_repo(tmp_path)
    p1 = _make_patch(repo, tmp_path / "o1", "int usage;", "long usage;", "a")
    p2 = _make_patch(repo, tmp_path / "o2", "int num_layers;", "long num_layers;", "b")
    root = tmp_path / "patches"
    _make_set(root, "a", [p1], '[config]\nSECURITY_APPARMOR_RESTRICT_USERNS = "n"\n')
    _make_set(root, "b", [p2], '[config]\nCONFIG_FOO = "y"\n')
    sets = pm.resolve(["a", "b"], root)
    assert pm.merged_config(sets) == {
        "CONFIG_SECURITY_APPARMOR_RESTRICT_USERNS": "n",
        "CONFIG_FOO": "y",
    }


def test_set_config_conflict(tmp_path):
    repo = _kernel_repo(tmp_path)
    p1 = _make_patch(repo, tmp_path / "o1", "int usage;", "long usage;", "a")
    p2 = _make_patch(repo, tmp_path / "o2", "int num_layers;", "long num_layers;", "b")
    root = tmp_path / "patches"
    _make_set(root, "a", [p1], '[config]\nFOO = "y"\n')
    _make_set(root, "b", [p2], '[config]\nFOO = "n"\n')
    with pytest.raises(pm.PatchError, match="CONFIG_FOO"):
        pm.merged_config(pm.resolve(["a", "b"], root))


def test_apply_config_and_mismatch(tmp_path):
    cfg = tmp_path / ".config"
    cfg.write_text("CONFIG_A=y\nCONFIG_B=y\n# CONFIG_C is not set\n")
    want = {"CONFIG_B": "n", "CONFIG_C": "y"}
    pm.apply_config(cfg, want)
    lines = cfg.read_text().splitlines()
    assert (
        "CONFIG_A=y" in lines
        and "# CONFIG_B is not set" in lines
        and "CONFIG_C=y" in lines
    )
    assert pm.config_mismatches(cfg, want) == []
    cfg.write_text("CONFIG_B=y\n")  # olddefconfig "re-enabled" B, dropped C
    assert len(pm.config_mismatches(cfg, want)) == 2


def test_patch_subject_unfolds_continuation_lines(tmp_path):
    p = tmp_path / "x.patch"
    p.write_text(
        "From abc Mon Sep 17 00:00:00 2001\n"
        "Subject: [PATCH 7/8] UBUNTU: SAUCE: apparmor5.0.0 [18/57]: apparmor: Use\n"
        " AA_SFS_TYPE_BOOLEAN_INTPRINT for userns\n"
        "\n"
        "body\n"
    )
    assert pm._patch_subject(p) == (
        "UBUNTU: SAUCE: apparmor5.0.0 [18/57]: apparmor: Use AA_SFS_TYPE_BOOLEAN_INTPRINT for userns"
    )
