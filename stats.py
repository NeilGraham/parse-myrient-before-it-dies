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
import glob as glob_mod
import math
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
# File filtering — release parsing
# ---------------------------------------------------------------------------

_TYPE_PRIORITY = {"official": 0, "beta": 1, "proto": 2, "demo": 3, "sample": 4}
_PAREN_RE = re.compile(r"\(([^)]+)\)")
_BASE_RE = re.compile(r"^(.*?)\s*\(")
_REV_RE = re.compile(r"^Rev\s+(\d+)$", re.IGNORECASE)
_DISC_RE = re.compile(r"^Disc\s+(\d+)", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BETA_RE = re.compile(r"^Beta(?:\s+(\d+))?$", re.IGNORECASE)
_PROTO_RE = re.compile(r"^Proto(?:\s+(\d+))?$", re.IGNORECASE)
_DEMO_RE = re.compile(r"^Demo(?:\s+(\d+))?$", re.IGNORECASE)
_SAMPLE_RE = re.compile(r"^Sample$", re.IGNORECASE)


def _parse_release(filename: str) -> dict:
    """Parse a ROM filename into structured components.

    Returns dict with keys: filename, base_title, regions, type,
    rev, date, disc, type_number.
    """
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    m = _BASE_RE.match(stem)
    base_title = m.group(1).strip() if m else stem.strip()

    groups = _PAREN_RE.findall(stem)

    regions: list[str] = []
    release_type = "official"
    rev = -1
    date = ""
    disc: int | None = None
    type_number = -1
    region_set = False

    for g in groups:
        g = g.strip()

        rm = _REV_RE.match(g)
        if rm:
            rev = int(rm.group(1))
            continue
        if _DISC_RE.match(g):
            disc = int(re.search(r"\d+", g).group())
            continue
        if _DATE_RE.match(g):
            date = g
            continue
        bm = _BETA_RE.match(g)
        if bm:
            release_type = "beta"
            type_number = int(bm.group(1)) if bm.group(1) else -1
            continue
        pm = _PROTO_RE.match(g)
        if pm:
            release_type = "proto"
            type_number = int(pm.group(1)) if pm.group(1) else -1
            continue
        dm = _DEMO_RE.match(g)
        if dm:
            release_type = "demo"
            type_number = int(dm.group(1)) if dm.group(1) else -1
            continue
        if _SAMPLE_RE.match(g):
            release_type = "sample"
            continue

        # First unclassified group is the region
        if not region_set:
            regions = [r.strip() for r in g.split(", ")] if ", " in g else [g]
            region_set = True

    return {
        "filename": filename,
        "base_title": base_title,
        "regions": regions,
        "type": release_type,
        "rev": rev,
        "date": date,
        "disc": disc,
        "type_number": type_number,
    }


def latest_revisions_filter(filenames: list[str], preferred_regions: list[str] = []) -> set[str]:
    """Return the subset of filenames to keep, selecting the best version per title.

    Priority: official release (highest Rev N) > Beta (latest date / highest
    number) > Proto (same) > Demo > Sample.

    When *preferred_regions* is given, among equally-versioned entries the first
    matching region wins.  Without it, all region variants of the best version
    are kept.  Multi-disc entries (Disc 1, Disc 2, …) are always kept together.
    """
    parsed = [_parse_release(f) for f in filenames]

    # Group by base title
    games: dict[str, list[dict]] = {}
    for p in parsed:
        games.setdefault(p["base_title"], []).append(p)

    keep: set[str] = set()

    for _title, entries in games.items():
        # 1. Best (lowest-numbered) type available
        best_type_val = min(_TYPE_PRIORITY[e["type"]] for e in entries)
        type_entries = [e for e in entries if _TYPE_PRIORITY[e["type"]] == best_type_val]

        # 2. Version sort key (higher tuple = better)
        def version_key(e: dict) -> tuple:
            if e["type"] == "official":
                return (e["rev"],)
            return (e["type_number"], e["date"])

        # 3. Narrow to preferred region (if given)
        if preferred_regions:
            for pref in preferred_regions:
                regional = [e for e in type_entries if pref in e["regions"]]
                if regional:
                    type_entries = regional
                    break

        # 4. Pick the best version among remaining candidates
        best_vk = max(version_key(e) for e in type_entries)

        # 5. Keep all entries with that version (preserves every disc)
        for e in type_entries:
            if version_key(e) == best_vk:
                keep.add(e["filename"])

    return keep


def file_matches(filename: str, finclude: list[str], fexclude: list[str]) -> bool:
    """Return True if filename passes the include/exclude regex filters."""
    if finclude and not all(re.search(p, filename) for p in finclude):
        return False
    if any(re.search(p, filename) for p in fexclude):
        return False
    return True


def dir_matches(rel_dir: str, dinclude: list[str], dexclude: list[str]) -> bool:
    """Return True if a relative directory path passes the include/exclude regex filters."""
    if dinclude and not any(re.search(p, rel_dir) for p in dinclude):
        return False
    if any(re.search(p, rel_dir) for p in dexclude):
        return False
    return True


# ---------------------------------------------------------------------------
# TSV walking
# ---------------------------------------------------------------------------

def resolve_root(path_or_url: str) -> Path:
    if path_or_url.startswith("http"):
        url = path_or_url if path_or_url.endswith("/") else path_or_url + "/"
        rel = url[len(BASE_URL):] if url.startswith(BASE_URL) else ""
        return OUTPUT_ROOT / Path(unquote(rel)) if rel else OUTPUT_ROOT
    return Path(path_or_url)


def collect_stats(root: Path, finclude: list[str] = [], fexclude: list[str] = [], dinclude: list[str] = [], dexclude: list[str] = [], latest_revisions: bool = False, preferred_regions: list[str] = []) -> tuple[int, int, int, int]:
    """Return (file_count, dir_count, total_bytes, max_depth) by walking metadata.tsv files."""
    files = dirs = total_bytes = max_depth = 0

    for tsv_path in root.rglob("metadata.tsv"):
        depth = len(tsv_path.relative_to(root).parts) - 1
        if depth > max_depth:
            max_depth = depth
        rel_dir = "/".join(tsv_path.parent.relative_to(root).parts)
        if rel_dir and not dir_matches(rel_dir, dinclude, dexclude):
            continue
        with open(tsv_path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh, delimiter="\t"))

        file_rows = [r for r in rows if r.get("File Size", "-") != "-"]
        dirs += len(rows) - len(file_rows)

        file_rows = [r for r in file_rows if file_matches(r["File Name"], finclude, fexclude)]
        if latest_revisions:
            keep = latest_revisions_filter([r["File Name"] for r in file_rows], preferred_regions)
            file_rows = [r for r in file_rows if r["File Name"] in keep]

        files += len(file_rows)
        total_bytes += sum(parse_size(r.get("File Size", "")) for r in file_rows)

    return files, dirs, total_bytes, max_depth


def build_tree(directory: Path, name: str, rel_path: str, finclude: list[str] = [], fexclude: list[str] = [], dinclude: list[str] = [], dexclude: list[str] = [], latest_revisions: bool = False, preferred_regions: list[str] = []) -> dict:
    """Recursively build a stats tree for a directory node."""
    local_files = local_dirs = local_bytes = local_downloaded = 0
    children = []

    tsv_path = directory / "metadata.tsv"
    if tsv_path.exists():
        with open(tsv_path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh, delimiter="\t"))

        dir_rows = [r for r in rows if r.get("File Size", "-") == "-"]
        file_rows = [r for r in rows if r.get("File Size", "-") != "-"]
        local_dirs = len(dir_rows)

        for row in dir_rows:
            child_name = row.get("File Name", "").rstrip("/")
            child_path = directory / child_name
            child_rel = f"{rel_path}/{child_name}" if rel_path else child_name
            if child_path.is_dir() and dir_matches(child_rel, dinclude, dexclude):
                children.append(build_tree(child_path, child_name, child_rel, finclude, fexclude, dinclude, dexclude, latest_revisions, preferred_regions))

        file_rows = [r for r in file_rows if file_matches(r["File Name"], finclude, fexclude)]
        if latest_revisions:
            keep = latest_revisions_filter([r["File Name"] for r in file_rows], preferred_regions)
            file_rows = [r for r in file_rows if r["File Name"] in keep]

        for row in file_rows:
            local_files += 1
            local_bytes += parse_size(row.get("File Size", ""))
            if (directory / row["File Name"]).exists():
                local_downloaded += 1

    total_files = local_files + sum(c["total_files"] for c in children)
    total_downloaded = local_downloaded + sum(c["downloaded_files"] for c in children)
    return {
        "name": name,
        "total_files": total_files,
        "total_dirs": local_dirs + sum(c["total_dirs"] for c in children),
        "total_bytes": local_bytes + sum(c["total_bytes"] for c in children),
        "downloaded_files": total_downloaded,
        "children": children,
    }


def fmt_pct(downloaded: int, total: int) -> str:
    if total == 0:
        return "  -%"
    if downloaded == total:
        return "100%"
    return f"{math.floor(downloaded / total * 100):>3}%"


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
    pct_col   = fmt_pct(node['downloaded_files'], node['total_files'])
    files_col = f"{node['total_files']:>{fw},} files"
    dirs_col  = f"{node['total_dirs']:>{dw},} dirs"
    size_col  = f"{fmt_size(node['total_bytes']):>{sw}}"
    return f"{pct_col} │ {files_col} │ {dirs_col} │ {size_col}  {tree_part}"


def sort_tree(node: dict, sort_by: list[str]) -> None:
    """Recursively sort children of node in-place by one or more keys."""
    def key(c: dict) -> tuple:
        parts = []
        for k in sort_by:
            if k == "name":
                parts.append(c["name"])
            elif k == "files":
                parts.append(-c["total_files"])
            elif k == "dirs":
                parts.append(-c["total_dirs"])
            elif k == "size":
                parts.append(-c["total_bytes"])
            elif k == "done":
                parts.append(-(c["downloaded_files"] / c["total_files"]) if c["total_files"] else 1.0)
        return tuple(parts)
    node["children"].sort(key=key)
    for child in node["children"]:
        sort_tree(child, sort_by)


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
        "paths",
        nargs="*",
        default=[str(OUTPUT_ROOT)],
        metavar="PATH_OR_URL",
        help="local files/ paths or Myrient URLs to inspect (default: files/)",
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
    parser.add_argument(
        "--sort-by",
        nargs="+",
        choices=["done", "files", "dirs", "size", "name"],
        default=None,
        metavar="TYPE",
        help="sort children in --expanded tree by one or more keys: done, files, dirs, size, name"
             " (descending except name); e.g. --sort-by done size name",
    )
    parser.add_argument(
        "--finclude", nargs="+", action="extend", metavar="PATTERN", default=[],
        help="only count files whose name matches ALL of these regexes; may be specified multiple times",
    )
    parser.add_argument(
        "--fexclude", nargs="+", action="extend", metavar="PATTERN", default=[],
        help="exclude files whose name matches any of these regexes (applied after --finclude); may be specified multiple times",
    )
    parser.add_argument(
        "--dinclude", nargs="+", metavar="PATTERN", default=[],
        help="only count files in directories whose path (relative to root) matches any of these regexes",
    )
    parser.add_argument(
        "--dexclude", nargs="+", metavar="PATTERN", default=[],
        help="exclude directories whose path (relative to root) matches any of these regexes (applied after --dinclude)",
    )
    parser.add_argument(
        "--latest-revisions",
        action="store_true",
        help="for each title, keep only the best version using priority: "
             "official release (highest Rev N) > Beta (latest date/number) > "
             "Proto (latest date/number) > Demo > Sample. "
             "Applied after --finclude/--fexclude.",
    )
    parser.add_argument(
        "--preferred-regions",
        nargs="+",
        metavar="REGION",
        default=[],
        help="when used with --latest-revisions, prefer these regions in order "
             "(e.g. --preferred-regions World USA); if no preferred region is "
             "available, the best remaining region is kept",
    )
    args = parser.parse_args()

    filtering = bool(args.finclude or args.fexclude or args.dinclude or args.dexclude or args.latest_revisions or args.preferred_regions)

    # Expand glob patterns for local paths; URLs pass through unchanged
    raw_paths: list[str] = []
    for p in args.paths:
        if p.startswith("http"):
            raw_paths.append(p)
        else:
            matches = sorted(glob_mod.glob(p))
            raw_paths.extend(matches if matches else [p])

    if args.crawl:
        for raw in raw_paths:
            run_crawl(raw)

    # Resolve roots and derive Myrient URLs
    entries: list[tuple[str, Path, str]] = []
    for raw in raw_paths:
        root = resolve_root(raw)
        if not root.exists():
            print(f"Error: path does not exist: {root}", file=sys.stderr)
            sys.exit(1)
        if raw.startswith("http"):
            myrient_url = raw if raw.endswith("/") else raw + "/"
        else:
            try:
                rel = root.resolve().relative_to(OUTPUT_ROOT.resolve())
                myrient_url = BASE_URL + "/".join(quote(p, safe="") for p in rel.parts) + "/"
            except ValueError:
                myrient_url = BASE_URL
        entries.append((raw, root, myrient_url))

    # Per-path summary
    grand_files = grand_files_total = grand_bytes = grand_bytes_total = 0
    for i, (_raw, root, myrient_url) in enumerate(entries):
        if len(entries) > 1:
            print(f"\n[{i + 1} / {len(entries)}]")
        file_count, dir_count, total_bytes, max_depth = collect_stats(root, args.finclude, args.fexclude, args.dinclude, args.dexclude, args.latest_revisions, args.preferred_regions)
        if filtering:
            total_file_count, _, total_bytes_all, _ = collect_stats(root)
        else:
            total_file_count, total_bytes_all = file_count, total_bytes
        print(f"Local path  : {root.resolve()}")
        print(f"Myrient URL : {myrient_url}")
        if file_count != total_file_count:
            print(f"Files       : {file_count:,} selected  /  {total_file_count:,} total")
        else:
            print(f"Files       : {file_count:,}")
        print(f"Directories : {dir_count:,}")
        print(f"Max depth   : {max_depth}")
        if total_bytes != total_bytes_all:
            print(f"Total size  : {fmt_size(total_bytes)} selected ({total_bytes:,} bytes)  /  {fmt_size(total_bytes_all)} total ({total_bytes_all:,} bytes)")
        else:
            print(f"Total size  : {fmt_size(total_bytes)}  ({total_bytes:,} bytes)")
        grand_files       += file_count
        grand_files_total += total_file_count
        grand_bytes       += total_bytes
        grand_bytes_total += total_bytes_all

    if len(entries) > 1:
        print()
        if grand_files != grand_files_total:
            print(f"Grand total : {grand_files:,} selected  /  {grand_files_total:,} total files")
            print(f"Grand size  : {fmt_size(grand_bytes)} selected ({grand_bytes:,} bytes)  /  {fmt_size(grand_bytes_total)} total ({grand_bytes_total:,} bytes)")
        else:
            print(f"Grand total : {grand_files:,} files")
            print(f"Grand size  : {fmt_size(grand_bytes)} ({grand_bytes:,} bytes)")

    if args.expanded:
        trees = [
            build_tree(root, root.resolve().name, "", args.finclude, args.fexclude, args.dinclude, args.dexclude, args.latest_revisions, args.preferred_regions)
            for _raw, root, _url in entries
        ]

        if args.sort_by:
            for tree in trees:
                sort_tree(tree, args.sort_by)

        # Unified column widths across all trees
        col_widths = (0, 0, 0)
        for tree in trees:
            cw = _collect_col_widths(tree)
            col_widths = tuple(max(a, b) for a, b in zip(col_widths, cw))

        print()
        if len(trees) == 1:
            tree = trees[0]
            print(_row(tree, col_widths, tree["name"]))
            for i, child in enumerate(tree["children"]):
                print_tree(child, col_widths, "", i == len(tree["children"]) - 1)
        else:
            for i, tree in enumerate(trees):
                is_last = i == len(trees) - 1
                connector = "└── " if is_last else "├── "
                print(_row(tree, col_widths, f"{connector}{tree['name']}"))
                child_prefix = "    " if is_last else "│   "
                for j, child in enumerate(tree["children"]):
                    print_tree(child, col_widths, child_prefix, j == len(tree["children"]) - 1)


if __name__ == "__main__":
    main()
