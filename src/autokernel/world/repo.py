"""Flat, GPG-signed local apt repo + host adoption (pin 1001).

The repo is plain files: pool-less flat layout, ``Packages`` (and .gz —
apt requires the uncompressed checksum in Release to trust the index
at all; W0 learning), ``Release`` + ``InRelease``/``Release.gpg``
signed by a per-host throwaway key. Unattended signing needs
loopback pinentry, --no-tty, --yes, and a stale-agent kill — all W0
learnings encoded here.

``adopt`` wires the repo into the *host's* apt: keyring under
/usr/share/keyrings, a one-line sources.list.d entry, and an
origin pin at 1001 so stock never silently displaces a rebuild
(docs/WORLD.md: the watcher closes version gaps, not apt). Dry-run by
default; execute shells out via sudo.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ORIGIN = "autokernel-world"
KEYRING_NAME = "autokernel-world-keyring.gpg"
HOST_KEYRING = Path("/usr/share/keyrings") / KEYRING_NAME
HOST_SOURCES = Path("/etc/apt/sources.list.d/autokernel-world.list")
HOST_PREFERENCES = Path("/etc/apt/preferences.d/autokernel-world")
PIN_PRIORITY = 1001

_GPG_BASE = ["gpg", "--batch", "--no-tty", "--yes"]
_GPG_SIGN = [*_GPG_BASE, "--pinentry-mode", "loopback", "--passphrase", ""]


def _gpg_env(gnupg_dir: Path) -> dict[str, str]:
    import os

    return {**os.environ, "GNUPGHOME": str(gnupg_dir)}


def ensure_key(gnupg_dir: Path, keyring_out: Path) -> None:
    """Create the per-host signing key on first use; export the public
    keyring next to the repo so chroots and `adopt` can pick it up."""
    env = _gpg_env(gnupg_dir)
    if not gnupg_dir.exists():
        gnupg_dir.mkdir(parents=True, mode=0o700)
        (gnupg_dir / "gpg-agent.conf").write_text(
            "allow-loopback-pinentry\n", encoding="utf-8"
        )
        subprocess.run(
            [
                *_GPG_SIGN,
                "--quick-generate-key",
                f"autokernel world <world@{ORIGIN}.local>",
                "ed25519",
                "sign",
                "never",
            ],
            env=env,
            capture_output=True,
            check=True,
        )
    # A stale agent may predate gpg-agent.conf (W0 learning).
    subprocess.run(
        ["gpgconf", "--kill", "gpg-agent"], env=env, capture_output=True, check=False
    )
    keyring_out.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [*_GPG_BASE, "--export"], env=env, capture_output=True, check=True
    )
    keyring_out.write_bytes(result.stdout)


def publish(
    repo_dir: Path,
    debs: list[Path],
    *,
    gnupg_dir: Path,
    arch: str,
    runner=subprocess.run,
) -> None:
    """Copy .debs into the flat repo and regenerate signed indices."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    for deb in debs:
        target = repo_dir / deb.name
        if not target.exists():
            shutil.copy2(deb, target)
    reindex(repo_dir, gnupg_dir=gnupg_dir, arch=arch, runner=runner)


def reindex(
    repo_dir: Path, *, gnupg_dir: Path, arch: str, runner=subprocess.run
) -> None:
    packages = runner(
        ["apt-ftparchive", "packages", "."],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    (repo_dir / "Packages").write_text(packages.stdout, encoding="utf-8")
    runner(
        ["gzip", "-9kf", "Packages"],
        cwd=repo_dir,
        capture_output=True,
        check=True,
    )
    release = runner(
        [
            "apt-ftparchive",
            f"-o=APT::FTPArchive::Release::Origin={ORIGIN}",
            f"-o=APT::FTPArchive::Release::Label={ORIGIN}",
            f"-o=APT::FTPArchive::Release::Architectures={arch}",
            "release",
            ".",
        ],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    (repo_dir / "Release").write_text(release.stdout, encoding="utf-8")
    env = _gpg_env(gnupg_dir)
    runner(
        [*_GPG_SIGN, "--detach-sign", "--armor", "--output", "Release.gpg", "Release"],
        cwd=repo_dir,
        env=env,
        capture_output=True,
        check=True,
    )
    runner(
        [*_GPG_SIGN, "--clearsign", "--output", "InRelease", "Release"],
        cwd=repo_dir,
        env=env,
        capture_output=True,
        check=True,
    )


# ── adopt ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AdoptStep:
    description: str
    argv: list[str]
    stdin: str | None = None


@dataclass(frozen=True)
class AdoptPlan:
    repo_dir: Path
    steps: list[AdoptStep] = field(default_factory=list)


def adopt_plan(repo_dir: Path) -> AdoptPlan:
    keyring = repo_dir / KEYRING_NAME
    sources_line = f"deb [signed-by={HOST_KEYRING}] file://{repo_dir} ./\n"
    preferences = f"Package: *\nPin: release o={ORIGIN}\nPin-Priority: {PIN_PRIORITY}\n"
    return AdoptPlan(
        repo_dir=repo_dir,
        steps=[
            AdoptStep(
                description=f"install signing keyring → {HOST_KEYRING}",
                argv=["sudo", "install", "-m", "0644", str(keyring), str(HOST_KEYRING)],
            ),
            AdoptStep(
                description=f"add apt source → {HOST_SOURCES}",
                argv=["sudo", "tee", str(HOST_SOURCES)],
                stdin=sources_line,
            ),
            AdoptStep(
                description=f"pin origin {ORIGIN} at {PIN_PRIORITY} → {HOST_PREFERENCES}",
                argv=["sudo", "tee", str(HOST_PREFERENCES)],
                stdin=preferences,
            ),
            AdoptStep(
                description="apt update",
                argv=["sudo", "apt-get", "update"],
            ),
        ],
    )


def adopt_execute(plan: AdoptPlan, *, runner=subprocess.run) -> int:
    """Run the adopt steps; returns the first non-zero exit code (0 if
    all succeeded). Inherits stdio so the user sees the sudo prompt."""
    for step in plan.steps:
        proc = runner(
            step.argv,
            input=step.stdin.encode() if step.stdin else None,
            check=False,
        )
        if proc.returncode != 0:
            return proc.returncode
    return 0
