"""Command-line entry point for the bounded classical QChess engine."""

from __future__ import annotations

import argparse

if __package__:
    from .uci import run_uci
else:  # Also support: python src/qchess.py --mode uci
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.uci import run_uci


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run QChess's UCI interface")
    parser.add_argument("--mode", choices=("uci",), default="uci")
    parser.parse_args(argv)
    return run_uci()


if __name__ == "__main__":
    raise SystemExit(main())
