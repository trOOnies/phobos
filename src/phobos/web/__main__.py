"""``python -m phobos.web`` / ``phobos-web``: run the web app."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn

from phobos.config import load_settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="phobos-web", description=__doc__)
    parser.add_argument("--config", help="path to a YAML config file")
    parser.add_argument("--host", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, help="port (default 8005)")
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    args = parser.parse_args(argv)

    if args.config:
        # The factory re-reads settings in the (possibly reloaded) worker process.
        os.environ["PHOBOS_CONFIG"] = args.config
    settings = load_settings(args.config, host=args.host, port=args.port)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")

    print(f"Phobos on http://localhost:{settings.port}  (library: {settings.library_dir})")
    uvicorn.run(
        "phobos.web.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=args.reload,
        reload_dirs=[str(Path(__file__).parent.parent)] if args.reload else None,
    )


if __name__ == "__main__":
    main()
