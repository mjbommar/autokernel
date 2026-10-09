"""Carried kernel patch sets ("patch queue").

autokernel builds vanilla kernels, but sometimes a host needs a patch that
is not (yet) upstream: a posted fix waiting for merge, or a long-lived
out-of-tree patch such as Ubuntu's AppArmor userns restriction. Patch sets
make those explicit, versioned, and recorded in build provenance instead of
living as an ad-hoc branch in a kernel worktree.

Layout (one directory per set, under :func:`default_root`)::

    patches/<set>/meta.toml     # description, kind, version range, per-patch status
    patches/<set>/series        # ordered patch filenames (quilt-compatible)
    patches/<set>/0001-*.patch  # `git format-patch` output

``meta.toml``::

    description = "Landlock __counted_by FORTIFY panic workaround"
    kind = "upstream-pending"          # upstream-pending | out-of-tree | backport
    min_version = "7.3-rc1"            # inclusive; optional
    max_version = "7.4"                # exclusive; optional

    [patches."0001-landlock-....patch"]
    status = "posted"                  # posted | accepted | merged | out-of-tree
    link = "https://lore.kernel.org/..."
    upstream_subject = "landlock: Move the domain layer counter out of the anonymous union"

    [config]                           # optional: Kconfig the set requires,
    SECURITY_APPARMOR_RESTRICT_USERNS = "n"   # applied before olddefconfig

Application uses ``git am --3way`` on a fresh branch in the kernel source
git tree. A patch whose reverse already applies cleanly is reported as
``present`` and skipped, so a tree that already carries the fix (or an
upstream release that merged it) does not fail the build.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

VALID_KINDS = ("upstream-pending", "out-of-tree", "backport")
VALID_STATUSES = ("posted", "accepted", "merged", "out-of-tree")

_GIT_IDENT = ["-c", "user.name=autokernel", "-c", "user.email=autokernel@localhost"]


class PatchError(Exception):
    """A patch set is malformed or could not be applied.

    ``outcomes`` is set when application stopped on a conflict, so callers
    can show which patches applied before it.
    """

    def __init__(self, message: str, outcomes: list[PatchOutcome] | None = None):
        super().__init__(message)
        self.outcomes: list[PatchOutcome] = outcomes or []


# ── kernel versions ─────────────────────────────────────────────────────────


@functools.total_ordering
@dataclass(frozen=True)
class KernelVersion:
    """Orderable kernel version. A release sorts after all of its -rcN."""

    major: int
    minor: int
    sub: int = 0
    rc: int | None = None

    @property
    def _key(self) -> tuple[int, int, int, int]:
        return (
            self.major,
            self.minor,
            self.sub,
            self.rc if self.rc is not None else 1_000_000,
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, KernelVersion):
            return NotImplemented
        return self._key < other._key

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}" + (f".{self.sub}" if self.sub else "")
        return base + (f"-rc{self.rc}" if self.rc is not None else "")


_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:-rc(\d+))?$")


def parse_version(text: str) -> KernelVersion:
    m = _VERSION_RE.match(text.strip())
    if not m:
        raise PatchError(
            f"unparseable kernel version {text!r} (expected e.g. 7.3, 7.3.1, 7.3-rc6)"
        )
    major, minor, sub, rc = m.groups()
    return KernelVersion(int(major), int(minor), int(sub or 0), int(rc) if rc else None)


def source_version(source_dir: Path) -> KernelVersion:
    """Read VERSION/PATCHLEVEL/SUBLEVEL/EXTRAVERSION from a kernel Makefile."""
    makefile = Path(source_dir) / "Makefile"
    vals: dict[str, str] = {}
    for line in makefile.read_text(errors="replace").splitlines()[:20]:
        m = re.match(r"^(VERSION|PATCHLEVEL|SUBLEVEL|EXTRAVERSION)\s*=\s*(.*)$", line)
        if m:
            vals[m[1]] = m[2].strip()
    try:
        major, minor = int(vals["VERSION"]), int(vals["PATCHLEVEL"])
    except (KeyError, ValueError) as e:
        raise PatchError(f"{makefile}: no VERSION/PATCHLEVEL") from e
    sub = int(vals.get("SUBLEVEL") or 0)
    rc_m = re.match(r"^-rc(\d+)", vals.get("EXTRAVERSION", ""))
    return KernelVersion(major, minor, sub, int(rc_m[1]) if rc_m else None)


# ── patch sets ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PatchMeta:
    status: str = ""
    link: str = ""
    upstream_subject: str = ""


@dataclass(frozen=True)
class PatchSet:
    name: str
    path: Path
    description: str
    kind: str
    patches: tuple[Path, ...]
    meta: dict[str, PatchMeta] = field(default_factory=dict)
    min_version: KernelVersion | None = None
    max_version: KernelVersion | None = None
    config: dict[str, str] = field(default_factory=dict)

    def applies_to(self, version: KernelVersion) -> bool:
        if self.min_version is not None and version < self.min_version:
            return False
        if self.max_version is not None and not version < self.max_version:
            return False
        return True

    @property
    def version_range(self) -> str:
        lo = str(self.min_version) if self.min_version else "*"
        hi = str(self.max_version) if self.max_version else "*"
        return f"[{lo}, {hi})"


def default_root() -> Path:
    """``$AUTOKERNEL_PATCHES_DIR``, else ``<repo>/patches`` next to ``src/``."""
    env = os.environ.get("AUTOKERNEL_PATCHES_DIR")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[2] / "patches"


def load_set(path: Path) -> PatchSet:
    path = Path(path)
    meta_path, series_path = path / "meta.toml", path / "series"
    if not meta_path.exists():
        raise PatchError(f"{path}: missing meta.toml")
    if not series_path.exists():
        raise PatchError(f"{path}: missing series")
    try:
        raw = tomllib.loads(meta_path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise PatchError(f"{meta_path}: {e}") from e

    kind = raw.get("kind", "")
    if kind not in VALID_KINDS:
        raise PatchError(f"{meta_path}: kind={kind!r}, expected one of {VALID_KINDS}")

    names = [
        ln.split("#", 1)[0].strip()
        for ln in series_path.read_text().splitlines()
        if ln.split("#", 1)[0].strip()
    ]
    if not names:
        raise PatchError(f"{series_path}: empty series")
    patches = []
    for n in names:
        p = path / n
        if not p.is_file():
            raise PatchError(f"{series_path}: listed patch {n!r} not found")
        patches.append(p)

    per_patch: dict[str, PatchMeta] = {}
    for fname, m in (raw.get("patches") or {}).items():
        if fname not in names:
            raise PatchError(f"{meta_path}: [patches.{fname!r}] is not in series")
        status = m.get("status", "")
        if status and status not in VALID_STATUSES:
            raise PatchError(
                f"{meta_path}: {fname}: status={status!r}, expected one of {VALID_STATUSES}"
            )
        per_patch[fname] = PatchMeta(
            status=status,
            link=m.get("link", ""),
            upstream_subject=m.get("upstream_subject", ""),
        )

    config: dict[str, str] = {}
    for sym, val in (raw.get("config") or {}).items():
        sym = sym if sym.startswith("CONFIG_") else f"CONFIG_{sym}"
        if not re.fullmatch(r"CONFIG_[A-Z0-9_]+", sym) or not isinstance(val, str):
            raise PatchError(
                f'{meta_path}: [config] {sym} = {val!r}: expected CONFIG_NAME = "y|m|n|<value>"'
            )
        config[sym] = val

    return PatchSet(
        name=path.name,
        path=path,
        description=raw.get("description", ""),
        config=config,
        kind=kind,
        patches=tuple(patches),
        meta=per_patch,
        min_version=parse_version(raw["min_version"])
        if raw.get("min_version")
        else None,
        max_version=parse_version(raw["max_version"])
        if raw.get("max_version")
        else None,
    )


def discover(root: Path | None = None) -> list[PatchSet]:
    root = Path(root) if root else default_root()
    if not root.is_dir():
        return []
    return [
        load_set(d)
        for d in sorted(root.iterdir())
        if d.is_dir() and (d / "series").exists()
    ]


def resolve(names: list[str], root: Path | None = None) -> list[PatchSet]:
    """Look up sets by name, preserving the requested order."""
    available = {s.name: s for s in discover(root)}
    missing = [n for n in names if n not in available]
    if missing:
        raise PatchError(
            f"unknown patch set(s): {', '.join(missing)}; available: {', '.join(sorted(available)) or '(none)'}"
        )
    return [available[n] for n in names]


# ── git plumbing ────────────────────────────────────────────────────────────


def _git(
    source_dir: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *_GIT_IDENT, "-C", str(source_dir), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise PatchError(
            f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def require_git_tree(source_dir: Path) -> None:
    if (
        _git(source_dir, "rev-parse", "--is-inside-work-tree", check=False).returncode
        != 0
    ):
        raise PatchError(
            f"{source_dir} is not a git work tree; patch sets need git (git am)"
        )


def tracked_changes(source_dir: Path) -> list[str]:
    """Modified tracked files (untracked build output is ignored)."""
    out = _git(source_dir, "status", "--porcelain", "--untracked-files=no").stdout
    return [ln for ln in out.splitlines() if ln.strip()]


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _patch_subject(p: Path) -> str:
    """Subject header, unfolded (RFC 5322 continuation lines start with whitespace)."""
    lines = p.read_text(errors="replace").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("Subject:"):
            parts = [line[len("Subject:") :].strip()]
            for cont in lines[i + 1 :]:
                if not cont[:1].isspace():
                    break
                parts.append(cont.strip())
            return re.sub(r"^(\[[^\]]*\]\s*)+", "", " ".join(parts)).strip()
    return ""


# ── check / apply ───────────────────────────────────────────────────────────


@dataclass
class PatchOutcome:
    set_name: str
    file: str
    subject: str
    sha256: str
    result: str  # "applied" | "present" | "conflict" | "would-apply"
    detail: str = ""
    status: str = ""
    link: str = ""

    def as_dict(self) -> dict[str, str]:
        return {k: v for k, v in self.__dict__.items() if v}


def _process(sets: list[PatchSet], tree: Path, *, dry_run: bool) -> list[PatchOutcome]:
    """Apply each patch in ``tree`` (already on the branch to modify).

    ``present`` = the reverse applies cleanly, i.e. the change is already in
    the tree; it is skipped. ``conflict`` stops processing.
    """
    outcomes: list[PatchOutcome] = []
    for ps in sets:
        for p in ps.patches:
            m = ps.meta.get(p.name, PatchMeta())
            o = PatchOutcome(
                set_name=ps.name,
                file=p.name,
                subject=_patch_subject(p),
                sha256=_sha256(p),
                result="",
                status=m.status,
                link=m.link,
            )
            outcomes.append(o)
            if (
                _git(
                    tree, "apply", "--check", "--reverse", str(p), check=False
                ).returncode
                == 0
            ):
                o.result = "present"
                continue
            am = _git(tree, "am", "--3way", "--keep-cr", str(p), check=False)
            if am.returncode != 0:
                _git(tree, "am", "--abort", check=False)
                o.result = "conflict"
                lines = (am.stdout + am.stderr).strip().splitlines()
                o.detail = next(
                    (ln for ln in lines if "error:" in ln or "patch failed" in ln),
                    lines[-1] if lines else "",
                )
                return outcomes
            o.result = "would-apply" if dry_run else "applied"
    return outcomes


def check(sets: list[PatchSet], source_dir: Path) -> list[PatchOutcome]:
    """Dry-run the series against HEAD in a throwaway worktree."""
    source_dir = Path(source_dir)
    require_git_tree(source_dir)
    with tempfile.TemporaryDirectory(prefix="autokernel-patchcheck-") as td:
        wt = Path(td) / "wt"
        _git(source_dir, "worktree", "add", "--detach", str(wt), "HEAD")
        try:
            return _process(sets, wt, dry_run=True)
        finally:
            _git(source_dir, "worktree", "remove", "--force", str(wt), check=False)


@dataclass
class ApplyResult:
    branch: str
    base_commit: str
    head_commit: str
    kernel_version: str
    outcomes: list[PatchOutcome]

    @property
    def ok(self) -> bool:
        return all(o.result in ("applied", "present") for o in self.outcomes)

    def provenance(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "kernel_version": self.kernel_version,
            "patches": [o.as_dict() for o in self.outcomes],
        }


def apply(
    sets: list[PatchSet],
    source_dir: Path,
    *,
    branch: str | None = None,
    allow_version_mismatch: bool = False,
) -> ApplyResult:
    """Apply ``sets`` onto a new branch at the tree's current HEAD.

    Requires a git work tree with no modified tracked files. On conflict the
    tree is restored to the original commit/branch and :class:`PatchError`
    is raised with the per-patch outcomes attached.
    """
    source_dir = Path(source_dir)
    require_git_tree(source_dir)
    dirty = tracked_changes(source_dir)
    if dirty:
        raise PatchError(
            f"{source_dir} has modified tracked files ({len(dirty)}), e.g. {dirty[0].strip()}; "
            "commit, stash, or reset them before applying patch sets"
        )
    kv = source_version(source_dir)
    if not allow_version_mismatch:
        wrong = [s for s in sets if not s.applies_to(kv)]
        if wrong:
            raise PatchError(
                "patch set(s) not declared for kernel "
                + f"{kv}: "
                + ", ".join(f"{s.name} {s.version_range}" for s in wrong)
            )

    base = _git(source_dir, "rev-parse", "HEAD").stdout.strip()
    orig_ref = (
        _git(
            source_dir, "symbolic-ref", "-q", "--short", "HEAD", check=False
        ).stdout.strip()
        or base
    )
    branch = branch or f"autokernel/{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    _git(source_dir, "checkout", "-q", "-B", branch, base)

    outcomes = _process(sets, source_dir, dry_run=False)
    head = _git(source_dir, "rev-parse", "HEAD").stdout.strip()
    result = ApplyResult(
        branch=branch,
        base_commit=base,
        head_commit=head,
        kernel_version=str(kv),
        outcomes=outcomes,
    )
    if not result.ok:
        _git(source_dir, "checkout", "-q", orig_ref, check=False)
        _git(source_dir, "branch", "-q", "-D", branch, check=False)
        bad = next(o for o in outcomes if o.result == "conflict")
        raise PatchError(
            f"{bad.set_name}/{bad.file} does not apply to {kv} ({base[:12]}): {bad.detail}",
            outcomes,
        )
    return result


def merged_config(sets: list[PatchSet]) -> dict[str, str]:
    """Union of the sets' ``[config]`` tables; conflicting values are an error."""
    out: dict[str, str] = {}
    origin: dict[str, str] = {}
    for ps in sets:
        for sym, val in ps.config.items():
            if sym in out and out[sym] != val:
                raise PatchError(
                    f"{sym}: {origin[sym]} wants {out[sym]!r}, {ps.name} wants {val!r}"
                )
            out[sym], origin[sym] = val, ps.name
    return out


def apply_config(config_path: Path, overrides: dict[str, str]) -> None:
    """Set ``overrides`` in a .config in place (before olddefconfig)."""
    if not overrides:
        return
    lines = [
        ln
        for ln in Path(config_path).read_text().splitlines()
        if not any(
            ln.startswith(f"{s}=") or ln == f"# {s} is not set" for s in overrides
        )
    ]
    for sym, val in overrides.items():
        lines.append(f"# {sym} is not set" if val == "n" else f"{sym}={val}")
    Path(config_path).write_text("\n".join(lines) + "\n")


def config_mismatches(config_path: Path, overrides: dict[str, str]) -> list[str]:
    """Overrides that did not survive olddefconfig (e.g. unmet dependencies)."""
    have: dict[str, str] = {}
    for ln in Path(config_path).read_text().splitlines():
        m = re.match(r"^(CONFIG_[A-Z0-9_]+)=(.*)$", ln)
        if m:
            have[m[1]] = m[2]
        m = re.match(r"^# (CONFIG_[A-Z0-9_]+) is not set$", ln)
        if m:
            have[m[1]] = "n"
    return [
        f"{sym}: wanted {val!r}, got {have.get(sym, 'n')!r}"
        for sym, val in overrides.items()
        if have.get(sym, "n") != val
    ]
