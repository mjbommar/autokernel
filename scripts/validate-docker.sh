#!/usr/bin/env bash
set -euo pipefail

uv sync --frozen

uv run ruff check .
uv run ruff format --check .
uv run ty check
uvx bandit -q -c pyproject.toml -r src
uvx vulture --config pyproject.toml
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck -S warning scripts/collect.sh scripts/validate-docker.sh scripts/validate-qemu.sh install.sh
else
    uvx --from shellcheck-py shellcheck -S warning scripts/collect.sh scripts/validate-docker.sh scripts/validate-qemu.sh install.sh
fi
uv run pytest -q
