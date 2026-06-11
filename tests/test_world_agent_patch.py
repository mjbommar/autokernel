"""Tests for the tier-3 agentic-patch module (Phase 4). The CLI is
faked via an injected runner; git is real (operates on a tmp tree)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autokernel.world import agent_patch as ap


def _git_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "pkg-1.0"
    tree.mkdir()
    (tree / "foo.c").write_text("int x = 1;\n")
    return tree


def test_build_fix_prompt_has_log_and_flags():
    p = ap.build_fix_prompt(
        source="libfoo", version="1.2", flags_desc="-O3 -flto=thin", log_tail="boom"
    )
    assert "libfoo 1.2" in p and "-flto=thin" in p and "boom" in p


def test_claude_argv_shape():
    argv = ap._claude_argv("fix it", model="sonnet", max_turns=8, budget=1.0)
    assert argv[0] == "claude" and "--bare" in argv and "-p" in argv
    assert "--max-budget-usd" in argv and "1.00" in argv
    assert "--permission-mode" in argv and "dontAsk" in argv


def test_codex_argv_shape(tmp_path):
    argv = ap._codex_argv(
        "fix it", tmp_path, model="gpt-5.4", summary_file=tmp_path / "s.txt"
    )
    assert argv[:2] == ["codex", "exec"]
    assert "--sandbox" in argv and "workspace-write" in argv
    assert "--ephemeral" in argv


def test_claude_parse_extracts_result_and_cost():
    out = '{"result": "changed foo.c", "total_cost_usd": 0.12, "session_id": "x"}'
    summary, cost = ap._claude_parse(out, "")
    assert summary == "changed foo.c" and cost == 0.12


def test_codex_parse_extracts_agent_message():
    jsonl = (
        '{"type":"turn.started"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"patched x"}}\n'
        '{"type":"turn.completed","usage":{"output_tokens":10}}\n'
    )
    summary, _ = ap._codex_parse(jsonl, "")
    assert summary == "patched x"


def test_run_agent_captures_diff(tmp_path, monkeypatch):
    tree = _git_tree(tmp_path)
    monkeypatch.setattr(ap.shutil, "which", lambda _: "/usr/bin/claude")

    class _Proc:
        returncode = 0
        stdout = '{"result": "edited foo.c", "total_cost_usd": 0.05}'
        stderr = ""

    def fake_runner(argv, **kw):
        # simulate the agent editing the tree
        (tree / "foo.c").write_text("int x = 2;\n")
        return _Proc()

    res = ap.run_coding_agent("claude", tree, "fix", runner=fake_runner)
    assert res.ok and not res.gave_up
    assert "int x = 2;" in res.patch and "int x = 1;" in res.patch
    assert res.cost_usd == 0.05
    assert res.summary == "edited foo.c"


def test_run_agent_gave_up_on_no_diff(tmp_path, monkeypatch):
    tree = _git_tree(tmp_path)
    monkeypatch.setattr(ap.shutil, "which", lambda _: "/usr/bin/claude")

    class _Proc:
        returncode = 0
        stdout = '{"result": "could not fix"}'
        stderr = ""

    res = ap.run_coding_agent("claude", tree, "fix", runner=lambda *a, **k: _Proc())
    assert not res.ok and res.gave_up
    assert res.patch.strip() == ""


def test_run_agent_timeout(tmp_path, monkeypatch):
    tree = _git_tree(tmp_path)
    monkeypatch.setattr(ap.shutil, "which", lambda _: "/usr/bin/claude")

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout", 1))

    res = ap.run_coding_agent("claude", tree, "fix", runner=boom)
    assert not res.ok and res.timed_out and res.exit_code == 124


def test_run_agent_missing_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda _: None)
    res = ap.run_coding_agent("codex", _git_tree(tmp_path), "fix")
    assert not res.ok and res.exit_code == 127


def test_run_agent_rejects_unknown_backend(tmp_path):
    with pytest.raises(ValueError, match="unknown agent backend"):
        ap.run_coding_agent("gpt4all", _git_tree(tmp_path), "fix")


def test_save_patch(tmp_path):
    p = ap.save_patch("--- a\n+++ b\n", tmp_path / "patches", "libfoo")
    assert p.read_text().startswith("--- a")
    assert p.name == "libfoo.patch"


# ── agentic escalation wiring (triage_and_retry) ────────────────────────────


def test_agentic_patch_remedy_builds_override(tmp_path, monkeypatch):
    """agentic_patch_remedy fetches source, runs the agent, returns a
    patches= override. Fetch + agent are faked."""
    from datetime import UTC, datetime

    from autokernel.world import agent_patch
    from autokernel.world import builder as builder_mod
    from autokernel.world.models import (
        BaseRelease,
        BuildCost,
        BuildOutcome,
        GlobalFlags,
        PackageBuildRecord,
        Ring,
        SourceUnit,
        WorldManifest,
    )

    world_dir = tmp_path / "world"
    world_dir.mkdir()
    manifest = WorldManifest(
        created_at=datetime.now(UTC),
        host="h",
        base=BaseRelease(distro_id="ubuntu", suite="resolute", mirror="http://x",
                         components=["main"]),
        ring=Ring.REQUIRED,
        flags=GlobalFlags(compiler="clang", linker="bfd"),
        world=[],
    )  # fmt: skip
    ctx = builder_mod.BuildContext(
        manifest=manifest, world_dir=world_dir,
        chroot_tarball=tmp_path / "c.tar", apt_dir=tmp_path / "apt",
        repo_dir=tmp_path / "repo", gnupg_dir=tmp_path / "gpg",
        ccache_dir=None, jobs=1, publish_lock=__import__("threading").Lock(),
    )  # fmt: skip
    unit = SourceUnit(source="libfoo", version="1.0", binaries=["libfoo1"],
                      build_deps_in_closure=[], wave=0, cost=BuildCost.NORMAL)  # fmt: skip
    record = PackageBuildRecord(source="libfoo", archive_version="1.0",
                                flags_hash="x", outcome=BuildOutcome.FTBFS, wave=0)  # fmt: skip

    # fake the fetch: create an unpacked tree under the agentic scratch
    def fake_run(argv, **kw):
        if "source" in argv:
            tree = Path(kw["cwd"]) / "libfoo-1.0"
            tree.mkdir(parents=True, exist_ok=True)
            (tree / "x.c").write_text("int a=1;\n")
        return 0

    monkeypatch.setattr(builder_mod, "_run", fake_run)

    def fake_agent(backend, tree, prompt, **kw):
        return agent_patch.AgentResult(
            backend=backend, ok=True, gave_up=False,
            patch="--- a/x.c\n+++ b/x.c\n@@ -1 +1 @@\n-int a=1;\n+int a=2;\n",
            summary="fixed x.c", transcript="{}", cost_usd=0.1, exit_code=0,
        )  # fmt: skip

    monkeypatch.setattr(agent_patch, "run_coding_agent", fake_agent)

    remedy = builder_mod.agentic_patch_remedy(ctx, unit, record, backend="claude")
    assert remedy is not None
    assert remedy.patches and remedy.patches[0].endswith("libfoo.patch")
    assert "agentic patch (claude)" in remedy.reason
    assert (world_dir / "patches" / "libfoo.patch").exists()
    assert (world_dir / "patches" / "libfoo.transcript.json").exists()
