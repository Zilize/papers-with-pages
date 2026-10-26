"""Start the single-process local app."""

import argparse
import os
from pathlib import Path

import uvicorn

from app.server import create_app


def main():
    parser = argparse.ArgumentParser(description="Browse and collect paper resources locally")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--trusted-proxy", help="Explicit HTTP proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument(
        "--max-workers",
        type=int,
        choices=range(1, 17),
        default=8,
        help="Adaptive paper concurrency ceiling (default: 8)",
    )
    parser.add_argument(
        "--render-pages",
        type=int,
        choices=range(1, 17),
        default=6,
        help="Browser page concurrency ceiling (default: 6)",
    )
    args = parser.parse_args()
    uvicorn.run(
        create_app(
            args.data_dir,
            trusted_proxy=args.trusted_proxy,
            max_workers=args.max_workers,
            render_pages=args.render_pages,
            github_token=os.environ.get("PWP_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN"),
        ),
        host="127.0.0.1",
        port=args.port,
    )


if __name__ == "__main__":
    main()
