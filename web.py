"""Launch the QChess workbench on this computer's loopback interface."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from localServer import create_server
from workbench import claim, search, state


def create_app(port: int = 0):
    """Create the application with a fixed, preloaded set of public assets."""
    directory = Path(__file__).resolve().parent / "ui"
    assets = {
        "/": ("text/html; charset=utf-8", (directory / "index.html").read_bytes()),
        "/app.js": ("text/javascript; charset=utf-8", (directory / "app.js").read_bytes()),
        "/style.css": ("text/css; charset=utf-8", (directory / "style.css").read_bytes()),
        "/icon.svg": ("image/svg+xml", (directory / "icon.svg").read_bytes()),
        "/api/health": (
            "application/json; charset=utf-8",
            b'{"status":"ok","application":"qchess-workbench"}',
        ),
    }
    return create_server(port, assets, {"/api/state": state, "/api/search": search, "/api/claim": claim})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local QChess browser workbench.")
    parser.add_argument(
        "--port", type=int, default=0,
        help="local port, or 0 to choose an available port (default: 0)",
    )
    arguments = parser.parse_args(argv)
    if not 0 <= arguments.port <= 65535:
        parser.error("port must be between 0 and 65535")
    try:
        server = create_app(arguments.port)
    except OSError as error:
        parser.exit(1, f"Unable to start the local workbench: {error}\n")
    with server:
        print(f"QChess workbench: {server.origin}", flush=True)
        print("Open this address in your browser. Press Ctrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
