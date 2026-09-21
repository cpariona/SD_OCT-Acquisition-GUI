from __future__ import annotations

import argparse
import json
from pathlib import Path

from .storage import read_info


def inspect_file(path: Path) -> int:
    info = read_info(path)
    summary = {
        "path": str(info.path.resolve()),
        "complete": info.complete,
        "committed_alines": info.committed_alines,
        "expected_alines": info.expected_alines,
        "pixels_per_aline": info.pixels_per_aline,
        "available_shape": list(info.available_shape),
        "header": info.header,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="octoce", description="Herramientas OCT/OCE")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="Inspeccionar un archivo .bin")
    inspect_parser.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.command == "inspect":
        return inspect_file(args.path)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
