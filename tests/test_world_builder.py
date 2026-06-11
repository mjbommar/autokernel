"""Tests for the world builder + repo (W2). Subprocess-free: pure
functions (env composition, audit, sbuildrc, resume keying, adopt
plan) tested directly; runners injected where needed."""

from __future__ import annotations

from datetime import UTC, datetime

from autokernel.world import builder as builder_mod
from autokernel.world import repo as repo_mod
from autokernel.world.models import (
    AuditVerdict,
    BuildCost,
    BuildOutcome,
    GlobalFlags,
    Lto,
    OverrideSource,
    PackageBuildRecord,
    PackageOverride,
    SourceUnit,
)


def _flags(**kw) -> GlobalFlags:
    return GlobalFlags(march="native", opt="-O3", lto=Lto.AUTO, **kw)


def _override(**kw) -> PackageOverride:
    return PackageOverride(
        source_pkg="zlib",
        reason="test",
        provenance=OverrideSource.USER,
        **kw,
    )


def _unit(source="zlib", wave=0, use_stock=False) -> SourceUnit:
    return SourceUnit(
        source=source,
        version="1:1.3-2",
        binaries=["zlib1g"],
        build_deps_in_closure=[],
        wave=wave,
        cost=BuildCost.NORMAL,
        use_stock=use_stock,
    )


# ── flags / env composition ─────────────────────────────────────────────────


def test_effective_cflags_applies_override():
    flags = _flags()
    assert "-flto=auto" in builder_mod.effective_cflags(flags, None)
    stripped = builder_mod.effective_cflags(
        flags, _override(strip_flags=["-flto=auto"], add_flags=["-fno-lto"])
    )
    assert "-flto=auto" not in stripped
    assert stripped.endswith("-fno-lto")


def test_build_environment_composition():
    env = builder_mod.build_environment(
        GlobalFlags(build_options=["nocheck"], build_profiles=["nodoc"]),
        _override(profiles=["pkg.zlib.minimal"]),
        jobs=4,
        ccache_dir="/srv/world-ccache",
    )
    assert env["DEB_CFLAGS_APPEND"] == env["DEB_CXXFLAGS_APPEND"]
    assert "nocheck" in env["DEB_BUILD_OPTIONS"]
    assert "parallel=4" in env["DEB_BUILD_OPTIONS"]
    assert env["DEB_BUILD_PROFILES"] == "nodoc pkg.zlib.minimal"
    assert env["CCACHE_DIR"] == "/srv/world-ccache"


def test_flags_hash_changes_with_override():
    flags = _flags()
    base = builder_mod.flags_hash(flags, None)
    assert builder_mod.flags_hash(flags, None) == base  # deterministic
    assert builder_mod.flags_hash(flags, _override(strip_flags=["-O3"])) != base
    assert builder_mod.flags_hash(flags, _override(force_compiler="clang")) != base


# ── sbuildrc rendering ──────────────────────────────────────────────────────


def test_render_sbuildrc_minimal(tmp_path):
    rc = builder_mod.render_sbuildrc(env={"DEB_CFLAGS_APPEND": "-O3"}, ccache_dir=None)
    assert "$chroot_mode = 'unshare';" in rc
    assert "'DEB_CFLAGS_APPEND' => '-O3'" in rc
    assert "$unshare_bind_mounts = [];" in rc
    assert "/usr/lib/ccache" in rc  # harmless when absent


def test_render_sbuildrc_with_ccache(tmp_path):
    rc = builder_mod.render_sbuildrc(env={}, ccache_dir=tmp_path / "ccache")
    assert f"directory => '{tmp_path / 'ccache'}'" in rc
    assert "'CCACHE_DIR' => '/srv/world-ccache'" in rc


def test_extra_package_args_lists_published_debs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "b_1_amd64.deb").write_bytes(b"")
    (repo / "a_1_amd64.deb").write_bytes(b"")
    (repo / "Packages").write_text("", encoding="utf-8")  # not a .deb
    args = builder_mod.extra_package_args(repo)
    assert args == [
        f"--extra-package={repo / 'a_1_amd64.deb'}",
        f"--extra-package={repo / 'b_1_amd64.deb'}",
    ]


def test_extra_package_args_empty_repo(tmp_path):
    assert builder_mod.extra_package_args(tmp_path) == []


# ── audit semantics (W0 learnings) ──────────────────────────────────────────

_BLHC_FINDINGS = """\
NONVERBOSE BUILD: Building shared library libz.so
CFLAGS missing (-fPIE): gcc -O3 -march=native -c x.c
CFLAGS missing (-fPIE): gcc -O3 -march=native -c y.c
LDFLAGS missing (-fPIE -pie): gcc -o x x.o
"""


def test_audit_ok_with_informational_blhc():
    audit = builder_mod.audit_build_log(
        "gcc -march=native -O3 -c x.c",
        ["-march=native", "-O3"],
        blhc_output=_BLHC_FINDINGS,
        blhc_rc=1,
    )
    assert audit.verdict == AuditVerdict.OK
    assert audit.missing == []
    assert audit.blhc_finding_count == 4
    assert "CFLAGS missing (-fPIE)" in audit.blhc_summary


def test_audit_missing_flags_fails():
    audit = builder_mod.audit_build_log(
        "gcc -O2 -c x.c",
        ["-march=native", "-O3"],
        blhc_output="",
        blhc_rc=0,
    )
    assert audit.verdict == AuditVerdict.MISSING_FLAGS
    assert audit.missing == ["-march=native", "-O3"]


def test_audit_no_compiler_is_not_a_failure():
    audit = builder_mod.audit_build_log(
        "dh_install: data only",
        ["-march=native"],
        blhc_output="No compiler commands were found in the log.",
        blhc_rc=1,
    )
    assert audit.verdict == AuditVerdict.NO_COMPILER


# ── resume keying ───────────────────────────────────────────────────────────


def _record(unit: SourceUnit, fhash: str, outcome=BuildOutcome.OK):
    return PackageBuildRecord(
        source=unit.source,
        archive_version=unit.version,
        flags_hash=fhash,
        outcome=outcome,
        wave=unit.wave,
        finished_at=datetime.now(UTC),
    )


def test_needs_build_resume_logic(tmp_path):
    unit = _unit()
    assert builder_mod.needs_build(tmp_path, unit, "abc")  # no record
    builder_mod._save_record(tmp_path, unit, _record(unit, "abc"))
    assert not builder_mod.needs_build(tmp_path, unit, "abc")  # done
    assert builder_mod.needs_build(tmp_path, unit, "xyz")  # flags changed
    builder_mod._save_record(tmp_path, unit, _record(unit, "abc", BuildOutcome.FTBFS))
    assert builder_mod.needs_build(tmp_path, unit, "abc")  # failed → retry


def test_needs_build_stock_never_builds(tmp_path):
    assert not builder_mod.needs_build(tmp_path, _unit(use_stock=True), "abc")


def test_unit_dir_escapes_epoch(tmp_path):
    d = builder_mod.unit_dir(tmp_path, _unit())
    assert ":" not in d.name
    assert "%3a" in d.name


# ── adopt plan ──────────────────────────────────────────────────────────────


def test_adopt_plan_contents(tmp_path):
    plan = repo_mod.adopt_plan(tmp_path / "repo")
    rendered = "\n".join(f"{' '.join(s.argv)}\n{s.stdin or ''}" for s in plan.steps)
    assert "Pin-Priority: 1001" in rendered
    assert f"o={repo_mod.ORIGIN}" in rendered
    assert "signed-by=/usr/share/keyrings/autokernel-world-keyring.gpg" in rendered
    assert f"file://{tmp_path / 'repo'} ./" in rendered
    # Everything mutating goes through sudo; nothing writes /etc directly.
    assert all(s.argv[0] == "sudo" for s in plan.steps)


def test_adopt_execute_stops_on_failure():
    calls = []

    class _R:
        def __init__(self, rc):
            self.returncode = rc

    def runner(argv, **kw):
        calls.append(argv)
        return _R(1 if len(calls) == 2 else 0)

    plan = repo_mod.adopt_plan(repo_dir=__import__("pathlib").Path("/tmp/r"))
    rc = repo_mod.adopt_execute(plan, runner=runner)
    assert rc == 1
    assert len(calls) == 2  # stopped at the failing step


# ── clang plumbing ──────────────────────────────────────────────────────────


def test_clang_world_translates_lto_and_sets_compiler_env():
    flags = GlobalFlags(march="native", opt="-O3", lto=Lto.AUTO, compiler="clang")
    env = builder_mod.build_environment(flags, None, jobs=2, ccache_dir=None)
    assert "-flto=thin" in env["DEB_CFLAGS_APPEND"]
    assert "-flto=auto" not in env["DEB_CFLAGS_APPEND"]
    # DWARF4: Ubuntu's dwz can't parse clang's DWARF5 (dh_dwz hard-fails).
    assert "-gdwarf-4" in env["DEB_CFLAGS_APPEND"]
    assert env["CC"] == "clang" and env["CXX"] == "clang++"
    assert env["DEB_LDFLAGS_APPEND"] == "-flto=thin -fuse-ld=lld"
    # Distro gcc-LTO defaults dropped from dpkg-buildflags output.
    assert env["DEB_BUILD_MAINT_OPTIONS"] == "optimize=-lto"


def test_force_gcc_override_in_clang_world():
    flags = GlobalFlags(march="native", opt="-O3", lto=Lto.AUTO, compiler="clang")
    forced = _override(force_compiler="gcc")
    env = builder_mod.build_environment(flags, forced, jobs=2, ccache_dir=None)
    # gcc dialect for this one package, explicit CC since cc→clang isn't set
    assert "-flto=auto" in env["DEB_CFLAGS_APPEND"]
    assert env["CC"] == "gcc" and env["CXX"] == "g++"
    assert "DEB_LDFLAGS_APPEND" not in env
    assert builder_mod.flags_hash(flags, forced) != builder_mod.flags_hash(flags, None)


def test_gcc_world_env_unchanged_by_clang_plumbing():
    """Baseline stability: gcc worlds must not grow CC/LDFLAGS keys and
    the flags string must be byte-identical to the pre-clang behavior —
    otherwise every cached gcc build record is invalidated."""
    flags = GlobalFlags(march="native", opt="-O3", lto=Lto.AUTO)  # compiler=gcc
    env = builder_mod.build_environment(flags, None, jobs=2, ccache_dir=None)
    assert "CC" not in env and "CXX" not in env
    assert "DEB_LDFLAGS_APPEND" not in env
    assert env["DEB_CFLAGS_APPEND"] == "-march=native -O3 -flto=auto"


def test_strip_build_options_clears_profile_too():
    flags = GlobalFlags(
        build_options=["nocheck", "nodoc"], build_profiles=["nocheck", "nodoc"]
    )
    remedy = _override(strip_build_options=["nodoc"])
    env = builder_mod.build_environment(flags, remedy, jobs=1, ccache_dir=None)
    assert "nodoc" not in env["DEB_BUILD_OPTIONS"]
    # debhelper honors the profile as well as the option — both cleared.
    assert env["DEB_BUILD_PROFILES"] == "nocheck"
    assert builder_mod.flags_hash(flags, remedy) != builder_mod.flags_hash(flags, None)


def test_strip_lto_reaches_link_stage_in_clang_world():
    flags = GlobalFlags(march="native", opt="-O3", lto=Lto.AUTO, compiler="clang")
    remedy = _override(strip_flags=["-flto=thin"])
    env = builder_mod.build_environment(flags, remedy, jobs=1, ccache_dir=None)
    assert "-flto" not in env["DEB_CFLAGS_APPEND"]
    # The strip must clear the link stage too — and lld with it.
    assert "DEB_LDFLAGS_APPEND" not in env
    assert builder_mod.flags_hash(flags, remedy) != builder_mod.flags_hash(flags, None)


# ── linker selection (Phase 0 symver remedy) ────────────────────────────────


def test_linker_default_lld_for_clang():
    flags = GlobalFlags(march="native", opt="-O3", lto=Lto.AUTO, compiler="clang")
    env = builder_mod.build_environment(flags, None, jobs=1, ccache_dir=None)
    assert env["DEB_LDFLAGS_APPEND"] == "-flto=thin -fuse-ld=lld"


def test_linker_bfd_for_clang_world():
    flags = GlobalFlags(
        march="native", opt="-O3", lto=Lto.AUTO, compiler="clang", linker="bfd"
    )
    env = builder_mod.build_environment(flags, None, jobs=1, ccache_dir=None)
    assert env["DEB_LDFLAGS_APPEND"] == "-flto=thin -fuse-ld=bfd"
    # the bfd choice is part of the resume key
    base = GlobalFlags(march="native", opt="-O3", lto=Lto.AUTO, compiler="clang")
    assert builder_mod.flags_hash(flags, None) != builder_mod.flags_hash(base, None)


def test_explicit_linker_survives_lto_strip():
    """A symver remedy that strips -flto must keep an explicit bfd choice
    (unlike the implicit lld, which is dropped with LTO)."""
    flags = GlobalFlags(
        march="native", opt="-O3", lto=Lto.AUTO, compiler="clang", linker="bfd"
    )
    remedy = _override(strip_flags=["-flto=thin"])
    env = builder_mod.build_environment(flags, remedy, jobs=1, ccache_dir=None)
    assert "-flto" not in env.get("DEB_LDFLAGS_APPEND", "")
    assert env["DEB_LDFLAGS_APPEND"] == "-fuse-ld=bfd"


def test_gcc_world_unaffected_by_linker_field():
    flags = GlobalFlags(march="native", opt="-O3", lto=Lto.AUTO)  # gcc, no linker
    env = builder_mod.build_environment(flags, None, jobs=1, ccache_dir=None)
    assert "DEB_LDFLAGS_APPEND" not in env


# ── compiler masquerade + identity audit (Phase 0 §0.5) ─────────────────────


def test_compiler_identity_audit_detects_gcc_majority():
    # bzip2-shape: gcc did the bulk, clang nothing → violation.
    log = "".join(f"x86_64-linux-gnu-gcc -O2 -c b{i}.c -o b{i}.o\n" for i in range(5))
    ok, detail = builder_mod.compiler_identity_audit(log, "clang")
    assert not ok and "majority" in detail


def test_compiler_identity_audit_tolerates_gcc_helper():
    # libselinux-shape: clang did the bulk, gcc only a throwaway helper
    # (-aux-info) → still a clang package.
    log = (
        "".join(f"clang -c u{i}.c -o u{i}.o\n" for i in range(20))
        + "gcc -aux-info temp.aux -c proto.c -o proto.o\n"
    )
    ok, detail = builder_mod.compiler_identity_audit(log, "clang")
    assert ok, detail


def test_compiler_identity_audit_passes_pure_clang():
    log = "clang -march=native -c a.c -o a.o\nclang++ -c b.cc -o b.o\n"
    ok, detail = builder_mod.compiler_identity_audit(log, "clang")
    assert ok and "gcc=0" in detail


def test_compiler_identity_audit_vacuous_for_data_package():
    ok, _ = builder_mod.compiler_identity_audit("dh_install: copying files\n", "clang")
    assert ok


def test_compiler_identity_audit_masquerade_bare_gcc_is_clang():
    # Phase 1: bzip2's debian/rules forces CC=gcc, but the masquerade
    # redirects bare gcc → clang. "gcc -c" in the log is really clang.
    log = "gcc -march=native -flto=thin -c blocksort.c -o blocksort.o\n"
    ok, detail = builder_mod.compiler_identity_audit(log, "clang", masquerade=True)
    assert ok, detail


def test_compiler_identity_audit_masquerade_catches_bypass():
    # absolute-path or versioned gcc bypasses the masquerade → violation
    for bypass in (
        "/usr/bin/gcc -O2 -c a.c -o a.o\n",
        "gcc-15 -O2 -c a.c -o a.o\n",
        "/usr/bin/x86_64-linux-gnu-gcc -c a.c -o a.o\n",
    ):
        ok, detail = builder_mod.compiler_identity_audit(
            bypass, "clang", masquerade=True
        )
        assert not ok, f"should flag bypass: {bypass!r} ({detail})"


def test_masquerade_hook_links_gcc_names_to_clang():
    hook = builder_mod.masquerade_hook()
    assert "ak-masq/gcc" in hook and "/usr/bin/clang" in hook
    assert "ak-masq/g++" in hook and "/usr/bin/clang++" in hook
    assert "x86_64-linux-gnu-gcc" in hook


def test_render_sbuildrc_masquerade_prepends_path():
    rc_on = builder_mod.render_sbuildrc(env={}, ccache_dir=None, masquerade=True)
    rc_off = builder_mod.render_sbuildrc(env={}, ccache_dir=None, masquerade=False)
    assert "/usr/local/lib/ak-masq:/usr/lib/ccache" in rc_on
    assert "ak-masq" not in rc_off


def test_masquerade_in_flags_hash_only_for_clang():
    clang_masq = GlobalFlags(
        compiler="clang", linker="bfd", masquerade=True, lto=Lto.AUTO
    )
    clang_plain = GlobalFlags(compiler="clang", linker="bfd", lto=Lto.AUTO)
    assert builder_mod.flags_hash(clang_masq, None) != builder_mod.flags_hash(
        clang_plain, None
    )
    # a force_compiler=gcc package in a masquerade world isn't masqueraded
    forced = _override(force_compiler="gcc")
    h_forced = builder_mod.flags_hash(clang_masq, forced)
    h_forced_plain = builder_mod.flags_hash(clang_plain, forced)
    assert h_forced == h_forced_plain  # masquerade key absent for gcc-forced pkg


# ── override patch application (Phase 4 prerequisite) ───────────────────────


def test_apply_patches_adds_to_series(tmp_path):
    tree = tmp_path / "pkg-1.0"
    (tree / "debian" / "patches").mkdir(parents=True)
    (tree / "debian" / "patches" / "series").write_text("existing.patch\n")
    (tree / "foo.c").write_text("int x = 1;\n")
    # a patch that applies cleanly
    patch = tmp_path / "fix.patch"
    patch.write_text(
        "--- a/foo.c\n+++ b/foo.c\n@@ -1 +1 @@\n-int x = 1;\n+int x = 2;\n"
    )
    ok, detail = builder_mod.apply_patches(tree, [str(patch)])
    assert ok, detail
    series = (tree / "debian" / "patches" / "series").read_text()
    assert "existing.patch" in series
    assert "autokernel-fix.patch" in series
    assert (tree / "debian" / "patches" / "autokernel-fix.patch").exists()


def test_apply_patches_rejects_nonapplying(tmp_path):
    tree = tmp_path / "pkg-1.0"
    tree.mkdir()
    (tree / "foo.c").write_text("int x = 1;\n")
    bad = tmp_path / "bad.patch"
    bad.write_text("--- a/foo.c\n+++ b/foo.c\n@@ -1 +1 @@\n-int y = 9;\n+int y = 8;\n")
    ok, problem = builder_mod.apply_patches(tree, [str(bad)])
    assert not ok and "does not apply" in problem


def test_apply_patches_missing_file(tmp_path):
    tree = tmp_path / "pkg-1.0"
    tree.mkdir()
    ok, problem = builder_mod.apply_patches(tree, ["/nonexistent.patch"])
    assert not ok and "not found" in problem
