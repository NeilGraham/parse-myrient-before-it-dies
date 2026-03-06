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
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, unquote

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
        return OUTPUT_ROOT / Path(unquote(rel)) if rel else OUTPUT_ROOT
    return Path(path_or_url)


def collect_stats(root: Path) -> tuple[int, int, int, int]:
    """Return (file_count, dir_count, total_bytes, max_depth) by walking metadata.tsv files."""
    files = dirs = total_bytes = max_depth = 0

    for tsv_path in root.rglob("metadata.tsv"):
        depth = len(tsv_path.relative_to(root).parts) - 1
        if depth > max_depth:
            max_depth = depth
        with open(tsv_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                if row.get("File Size", "-") == "-":
                    dirs += 1
                else:
                    files += 1
                    total_bytes += parse_size(row.get("File Size", ""))

    return files, dirs, total_bytes, max_depth


def build_tree(directory: Path, name: str) -> dict:
    """Recursively build a stats tree for a directory node."""
    local_files = local_dirs = local_bytes = 0
    children = []

    tsv_path = directory / "metadata.tsv"
    if tsv_path.exists():
        with open(tsv_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                if row.get("File Size", "-") == "-":
                    local_dirs += 1
                    child_name = row.get("File Name", "").rstrip("/")
                    child_path = directory / child_name
                    if child_path.is_dir():
                        children.append(build_tree(child_path, child_name))
                else:
                    local_files += 1
                    local_bytes += parse_size(row.get("File Size", ""))

    return {
        "name": name,
        "total_files": local_files + sum(c["total_files"] for c in children),
        "total_dirs": local_dirs + sum(c["total_dirs"] for c in children),
        "total_bytes": local_bytes + sum(c["total_bytes"] for c in children),
        "children": children,
    }


def _collect_col_widths(node: dict) -> tuple[int, int, int]:
    """Return (max_files_w, max_dirs_w, max_size_w) across the whole tree."""
    fw = len(f"{node['total_files']:,}")
    dw = len(f"{node['total_dirs']:,}")
    sw = len(fmt_size(node['total_bytes']))
    for child in node["children"]:
        cf, cd, cs = _collect_col_widths(child)
        fw, dw, sw = max(fw, cf), max(dw, cd), max(sw, cs)
    return fw, dw, sw


def _row(node: dict, col_widths: tuple[int, int, int], tree_part: str) -> str:
    fw, dw, sw = col_widths
    files_col = f"{node['total_files']:>{fw},} files"
    dirs_col  = f"{node['total_dirs']:>{dw},} dirs"
    size_col  = f"{fmt_size(node['total_bytes']):>{sw}}"
    return f"{files_col} │ {dirs_col} │ {size_col}  {tree_part}"


def print_tree(node: dict, col_widths: tuple[int, int, int], prefix: str = "", is_last: bool = True) -> None:
    connector = "└── " if is_last else "├── "
    print(_row(node, col_widths, f"{prefix}{connector}{node['name']}"))
    child_prefix = prefix + ("    " if is_last else "│   ")
    children = node["children"]
    for i, child in enumerate(children):
        print_tree(child, col_widths, child_prefix, i == len(children) - 1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_crawl(path_or_url: str) -> None:
    """Run crawl.py against path_or_url to fill any missing metadata."""
    script = Path(__file__).parent / "crawl.py"
    subprocess.run(
        ["uv", "run", str(script), "--paths", path_or_url],
        check=False,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Myrient directory statistics")
    parser.add_argument(
        "path",
        nargs="?",
        default=str(OUTPUT_ROOT),
        metavar="PATH_OR_URL",
        help="local files/ path or Myrient URL to inspect (default: files/)",
    )
    parser.add_argument(
        "--crawl",
        action="store_true",
        help="run crawl.py first to fill any missing metadata before reporting stats",
    )
    parser.add_argument(
        "--expanded",
        action="store_true",
        help="print full directory tree with per-node file count, dir count, and size",
    )
    args = parser.parse_args()

    if args.crawl:
        run_crawl(args.path)

    root = resolve_root(args.path)
    if not root.exists():
        print(f"Error: path does not exist: {root}", file=sys.stderr)
        sys.exit(1)

    # Derive the Myrient URL from whichever form was provided
    raw = args.path
    if raw.startswith("http"):
        myrient_url = raw if raw.endswith("/") else raw + "/"
    else:
        try:
            rel = root.resolve().relative_to(OUTPUT_ROOT.resolve())
            myrient_url = BASE_URL + "/".join(quote(p, safe="") for p in rel.parts) + "/"
        except ValueError:
            myrient_url = BASE_URL

    file_count, dir_count, total_bytes, max_depth = collect_stats(root)

    print(f"Local path  : {root.resolve()}")
    print(f"Myrient URL : {myrient_url}")
    print(f"Files       : {file_count:,}")
    print(f"Directories : {dir_count:,}")
    print(f"Max depth   : {max_depth}")
    print(f"Total size  : {fmt_size(total_bytes)}  ({total_bytes:,} bytes)")

    if args.expanded:
        print()
        tree = build_tree(root, root.resolve().name)
        col_widths = _collect_col_widths(tree)
        print(_row(tree, col_widths, tree["name"]))
        children = tree["children"]
        for i, child in enumerate(children):
            print_tree(child, col_widths, "", i == len(children) - 1)


if __name__ == "__main__":
    main()
