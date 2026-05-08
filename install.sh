#!/usr/bin/env bash
# autokernel one-line bootstrap.
#
# Goal: get a user from "I have a fresh Linux box" to "I can run
#       `autokernel preflight`" in one curl.
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/<owner>/autokernel/main/install.sh | bash
#   curl -LsSf https://raw.githubusercontent.com/<owner>/autokernel/main/install.sh | bash -s -- --ref=v0.6
#   AUTOKERNEL_REPO=git@github.com:fork/autokernel.git ./install.sh
#
# What this does:
#   1. Sniffs the distro family for a friendly message.
#   2. Ensures `uv` is installed (using astral.sh's official installer).
#   3. Ensures `git` is on PATH (only hard requirement to clone).
#   4. Clones the repo to ~/.local/share/autokernel (or AUTOKERNEL_HOME).
#   5. Runs `uv sync` to populate the venv.
#   6. Drops a `~/.local/bin/autokernel` shim so the verb is on $PATH.
#   7. Suggests next steps.
#
# This script never sudo's. It never modifies /etc, /usr, or /boot.
# Everything lands under $HOME. If you'd rather work out of a clone in
# CWD, prefer:
#
#   git clone https://github.com/<owner>/autokernel
#   cd autokernel && uv sync
#   uv run autokernel preflight

set -euo pipefail

# ── config ─────────────────────────────────────────────────────────────────

REPO_URL="${AUTOKERNEL_REPO:-https://github.com/mjbommar/autokernel.git}"
INSTALL_DIR="${AUTOKERNEL_HOME:-$HOME/.local/share/autokernel}"
BIN_DIR="${AUTOKERNEL_BIN_DIR:-$HOME/.local/bin}"
REF=""
for arg in "$@"; do
    case "$arg" in
        --ref=*)  REF="${arg#--ref=}" ;;
        --help|-h)
            sed -n '1,30p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
    esac
done

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn()  { printf '\033[33m! %s\033[0m\n' "$*" >&2; }
err()   { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; }
ok()    { printf '\033[32m✓ %s\033[0m\n' "$*"; }
step()  { printf '\033[36m→ %s\033[0m\n' "$*"; }

# ── distro sniff (informational only) ───────────────────────────────────────

distro_id="unknown"
distro_pretty="unknown distro"
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    distro_id="${ID:-unknown}"
    distro_pretty="${PRETTY_NAME:-$ID}"
fi

bold "autokernel installer"
step "host: $distro_pretty"

# ── ensure uv ───────────────────────────────────────────────────────────────

if ! command -v uv >/dev/null 2>&1; then
    step "installing uv (the Python package/runtime manager)"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        err "neither curl nor wget on PATH; can't install uv"
        err "install one of them first, then re-run this script"
        exit 1
    fi
    # uv installs to ~/.local/bin by default; make sure that's on PATH for THIS session.
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
    err "uv install failed; re-open your shell or check ~/.local/bin is on PATH"
    exit 1
fi
ok "uv: $(uv --version)"

# ── ensure git ──────────────────────────────────────────────────────────────

if ! command -v git >/dev/null 2>&1; then
    err "git not on PATH"
    case "$distro_id" in
        ubuntu|debian|linuxmint|pop)  err "→ sudo apt install -y git" ;;
        fedora|rhel|centos|rocky|almalinux)  err "→ sudo dnf install -y git" ;;
        arch|manjaro)  err "→ sudo pacman -S --noconfirm git" ;;
        opensuse*|sles)  err "→ sudo zypper install -y git" ;;
        alpine)  err "→ sudo apk add git" ;;
        *)  err "→ install git via your distro's package manager" ;;
    esac
    exit 1
fi
ok "git: $(git --version | awk '{print $3}')"

# ── clone or update ─────────────────────────────────────────────────────────

mkdir -p "$(dirname "$INSTALL_DIR")"

if [ -d "$INSTALL_DIR/.git" ]; then
    step "updating existing clone at $INSTALL_DIR"
    git -C "$INSTALL_DIR" fetch --tags --quiet
    if [ -n "$REF" ]; then
        git -C "$INSTALL_DIR" checkout --quiet "$REF"
    else
        # Default branch tracking
        DEFAULT_REF="$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
        git -C "$INSTALL_DIR" pull --ff-only --quiet origin "$DEFAULT_REF" || \
            warn "couldn't fast-forward; leaving the working tree as-is"
    fi
else
    step "cloning $REPO_URL → $INSTALL_DIR"
    git clone --quiet "$REPO_URL" "$INSTALL_DIR"
    if [ -n "$REF" ]; then
        git -C "$INSTALL_DIR" checkout --quiet "$REF"
    fi
fi
ok "source at $INSTALL_DIR"

# ── populate venv ───────────────────────────────────────────────────────────

step "running 'uv sync' to install dependencies"
( cd "$INSTALL_DIR" && uv sync --quiet )
ok "venv populated"

# ── shim ────────────────────────────────────────────────────────────────────

mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/autokernel" <<EOF
#!/usr/bin/env bash
# autokernel shim — runs the package via uv from the install dir.
exec uv --project "$INSTALL_DIR" run autokernel "\$@"
EOF
chmod +x "$BIN_DIR/autokernel"
ok "shim at $BIN_DIR/autokernel"

# Make sure $BIN_DIR is on PATH for the user's next shell session.
case ":$PATH:" in
    *":$BIN_DIR:"*)  ;;
    *)  warn "$BIN_DIR is not on PATH; add this to your shell rc:"
        warn "    export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

# ── next steps ──────────────────────────────────────────────────────────────

cat <<EOF

$(bold "next:")
  $BIN_DIR/autokernel preflight                  # run pre-flight checks
  $BIN_DIR/autokernel scan /tmp/myhost           # snapshot this host
  $BIN_DIR/autokernel propose /tmp/myhost        # → proposal.json (needs an API key)

$(bold "API keys"): copy $INSTALL_DIR/.env.example to $INSTALL_DIR/.env and fill in
ANTHROPIC_API_KEY or OPENAI_API_KEY, OR export them in your shell.

EOF
