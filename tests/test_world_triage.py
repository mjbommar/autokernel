"""Tests for the W3 FTBFS triage layer. The LLM is never called: drafts
are constructed directly (the canned-failure-log → draft step is what
the live run validates); these tests pin override_check, verdict
promotion, the exceptions table, and log-tail extraction."""

from __future__ import annotations

from autokernel.world import triage as triage_mod
from autokernel.world.builder import build_environment, flags_hash
from autokernel.world.models import (
    FailureClass,
    GlobalFlags,
    Lto,
    OverrideSource,
    PackageOverride,
)

EFFECTIVE = ["-march=native", "-O3", "-flto=auto"]


def _draft(
    action: str, failure=FailureClass.TEST_FAILURE, **kw
) -> triage_mod._TriageDraft:
    return triage_mod._TriageDraft(
        failure_class=failure,
        remedy=triage_mod._RemedyDraft(action=action, reason="because", **kw),
        confidence=0.8,
        evidence=["FAIL: test_foo"],
    )


# ── override_check (the config_check analogue) ──────────────────────────────


def test_override_check_accepts_sane_nocheck():
    assert (
        triage_mod.override_check(_draft("nocheck"), effective_tokens=EFFECTIVE) == []
    )


def test_override_check_rejects_nocheck_for_miscompile():
    problems = triage_mod.override_check(
        _draft("nocheck", failure=FailureClass.OPT_MISCOMPILE),
        effective_tokens=EFFECTIVE,
    )
    assert any("opt-miscompile" in p for p in problems)


def test_override_check_rejects_hallucinated_strip_token():
    problems = triage_mod.override_check(
        _draft("strip-flags", strip_flags=["-fwhatever"]),
        effective_tokens=EFFECTIVE,
    )
    assert any("not in effective flags" in p for p in problems)


def test_override_check_accepts_real_strip_with_replacement():
    draft = _draft(
        "strip-flags",
        failure=FailureClass.OPT_MISCOMPILE,
        strip_flags=["-O3"],
        add_flags=["-O2"],
    )
    assert triage_mod.override_check(draft, effective_tokens=EFFECTIVE) == []


def test_override_check_rejects_garbage_add_flags():
    draft = _draft("strip-flags", strip_flags=["-O3"], add_flags=["rm -rf /"])
    problems = triage_mod.override_check(draft, effective_tokens=EFFECTIVE)
    assert any("doesn't look like a flag" in p for p in problems)


def test_override_check_rejects_unknown_action_and_stray_flags():
    assert triage_mod.override_check(_draft("yolo"), effective_tokens=EFFECTIVE)
    problems = triage_mod.override_check(
        _draft("retry", strip_flags=["-O3"]), effective_tokens=EFFECTIVE
    )
    assert any("action is 'retry'" in p for p in problems)


# ── draft → verdict promotion ───────────────────────────────────────────────


def test_draft_to_verdict_nocheck_sets_build_options():
    v = triage_mod.draft_to_verdict("acl", _draft("nocheck"))
    assert v.remedy is not None
    assert v.remedy.build_options == ["nocheck"]
    assert v.remedy.provenance == OverrideSource.LLM_TRIAGE


def test_draft_to_verdict_defer_has_no_remedy():
    v = triage_mod.draft_to_verdict("openssl", _draft("defer"))
    assert v.remedy is None


def test_draft_to_verdict_use_stock():
    v = triage_mod.draft_to_verdict("openssl", _draft("use-stock"))
    assert v.remedy is not None and v.remedy.use_stock


# ── remedies actually change the build (env + resume key) ───────────────────


def test_nocheck_remedy_changes_env_and_hash():
    flags = GlobalFlags(march="native", opt="-O3", lto=Lto.AUTO)
    remedy = triage_mod.draft_to_verdict("acl", _draft("nocheck")).remedy
    env = build_environment(flags, remedy, jobs=4, ccache_dir=None)
    assert "nocheck" in env["DEB_BUILD_OPTIONS"]
    assert flags_hash(flags, remedy) != flags_hash(flags, None)


def test_strip_remedy_changes_cflags():
    flags = GlobalFlags(march="native", opt="-O3", lto=Lto.AUTO)
    remedy = triage_mod.draft_to_verdict(
        "x",
        _draft(
            "strip-flags",
            failure=FailureClass.LTO_INCOMPAT,
            strip_flags=["-flto=auto"],
        ),
    ).remedy
    env = build_environment(flags, remedy, jobs=1, ccache_dir=None)
    assert "-flto=auto" not in env["DEB_CFLAGS_APPEND"]


# ── exceptions table ────────────────────────────────────────────────────────


def test_exceptions_round_trip_and_replace(tmp_path):
    a = PackageOverride(source_pkg="acl", build_options=["nocheck"], reason="r1")
    triage_mod.save_exception(tmp_path, a)
    b = PackageOverride(source_pkg="tar", strip_flags=["-O3"], reason="r2")
    triage_mod.save_exception(tmp_path, b)
    assert [o.source_pkg for o in triage_mod.load_exceptions(tmp_path)] == [
        "acl",
        "tar",
    ]
    # Same package again → replaced, not duplicated.
    a2 = PackageOverride(source_pkg="acl", use_stock=True, reason="r3")
    triage_mod.save_exception(tmp_path, a2)
    loaded = {o.source_pkg: o for o in triage_mod.load_exceptions(tmp_path)}
    assert len(loaded) == 2
    assert loaded["acl"].use_stock


# ── log-tail extraction ─────────────────────────────────────────────────────


def test_extract_log_tail_strips_apt_noise():
    log = "\n".join(
        ["Get:1 http://x foo", "Hit:2 http://y bar"] * 50
        + ["gcc -c x.c", "FAIL: test_mount needs CAP_SYS_ADMIN"]
    )
    tail = triage_mod.extract_log_tail(log, lines=10)
    assert "Get:1" not in tail
    assert "FAIL: test_mount" in tail


# ── strip-build-options remedy ──────────────────────────────────────────────


def test_override_check_strip_build_options():
    draft = _draft(
        "strip-build-options",
        failure=FailureClass.PACKAGING,
        strip_options=["nodoc"],
    )
    assert (
        triage_mod.override_check(
            draft, effective_tokens=EFFECTIVE, global_options=["nocheck", "nodoc"]
        )
        == []
    )
    # Token not actually in the global options → rejected.
    problems = triage_mod.override_check(
        draft, effective_tokens=EFFECTIVE, global_options=["nocheck"]
    )
    assert any("not in global build options" in p for p in problems)
    # Empty strip set → rejected.
    empty = _draft("strip-build-options", failure=FailureClass.PACKAGING)
    assert triage_mod.override_check(
        empty, effective_tokens=EFFECTIVE, global_options=["nodoc"]
    )


def test_draft_to_verdict_strip_build_options():
    v = triage_mod.draft_to_verdict(
        "sed",
        _draft(
            "strip-build-options",
            failure=FailureClass.PACKAGING,
            strip_options=["nodoc"],
        ),
    )
    assert v.remedy is not None
    assert v.remedy.strip_build_options == ["nodoc"]
