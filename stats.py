#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Myrient stats.
Reads local metadata.tsv files produced by crawl.py and prints aggregate
statistics: file count, directory count, and total size.

Accepts a local files/ path or a Myrient URL as the root to inspect.
"""

import argparse
import csv
import re
import sys
from pathlib import Path

BASE_URL = "https://myrient.erista.me/files/"
OUTPUT_ROOT = Path("files")

# ---------------------------------------------------------------------------
# Size parsing
# ---------------------------------------------------------------------------

_SIZE_UNITS = {
    "b":   1,
    "kib": 1024,
    "mib": 1024 ** 2,
    "gib": 1024 ** 3,
    "tib": 1024 ** 4,
    "kb":  1000,
    "mb":  1000 ** 2,
    "gb":  1000 ** 3,
    "tb":  1000 ** 4,
}
_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([a-z]+)\s*$", re.IGNORECASE)


def parse_size(s: str) -> int:
    """Return size in bytes, or 0 if unparseable/missing."""
    if not s or s.strip() == "-":
        return 0
    m = _SIZE_RE.match(s)
    if not m:
        return 0
    value, unit = float(m.group(1)), m.group(2).lower()
    return int(value * _SIZE_UNITS.get(unit, 0))


def fmt_size(n: int) -> str:
    for unit, threshold in [("TiB", 1024**4), ("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)]:
        if n >= threshold:
            return f"{n / threshold:.2f} {unit}"
    return f"{n} B"


# ---------------------------------------------------------------------------
# TSV walking
# ---------------------------------------------------------------------------

def resolve_root(path_or_url: str) -> Path:
    if path_or_url.startswith("http"):
        url = path_or_url if path_or_url.endswith("/") else path_or_url + "/"
        rel = url[len(BASE_URL):] if url.startswith(BASE_URL) else ""
        return OUTPUT_ROOT / Path(rel) if rel else OUTPUT_ROOT
    return Path(path_or_url)


def collect_stats(root: Path) -> tuple[int, int, int]:
    """Return (file_count, dir_count, total_bytes) by walking metadata.tsv files."""
    files = dirs = total_bytes = 0

    for tsv_path in root.rglob("metadata.tsv"):
        with open(tsv_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                if row.get("File Size", "-") == "-":
                    dirs += 1
                else:
                    files += 1
                    total_bytes += parse_size(row.get("File Size", ""))

    return files, dirs, total_bytes


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Myrient directory statistics")
    parser.add_argument(
        "path",
        nargs="?",
        default=str(OUTPUT_ROOT),
        metavar="PATH_OR_URL",
        help="local files/ path or Myrient URL to inspect (default: files/)",
    )
    args = parser.parse_args()

    root = resolve_root(args.path)
    if not root.exists():
        print(f"Error: path does not exist: {root}", file=sys.stderr)
        sys.exit(1)

    file_count, dir_count, total_bytes = collect_stats(root)

    print(f"Root        : {root.resolve()}")
    print(f"Files       : {file_count:,}")
    print(f"Directories : {dir_count:,}")
    print(f"Total size  : {fmt_size(total_bytes)}  ({total_bytes:,} bytes)")


if __name__ == "__main__":
    main()
