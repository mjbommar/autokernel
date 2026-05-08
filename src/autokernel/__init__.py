"""autokernel — LLM-assisted minimal Linux kernel builder."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("autokernel")
except PackageNotFoundError:
    __version__ = "0.15.0"


def main() -> None:
    from autokernel.cli import app

    app()
