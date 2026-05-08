"""autokernel — LLM-assisted minimal Linux kernel builder."""

__version__ = "0.1.0"


def main() -> None:
    from autokernel.cli import app

    app()
