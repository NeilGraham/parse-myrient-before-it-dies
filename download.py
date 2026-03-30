#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "beautifulsoup4",
#   "requests",
#   "rich",
#   "textual",
# ]
# ///
"""
Myrient downloader.
Ensures metadata is up to date via crawl.py, shows a stats overview,
confirms with the user, then downloads all files under the given path.

Resume-safe: skips complete files, resumes .part files via HTTP Range requests.
Stall-safe: timed-out or dropped connections are re-queued up to MAX_ATTEMPTS times.
"""

import argparse
import csv
import glob as glob_mod
import queue
import subprocess
import sys
import threading
from pathlib import Path

import requests
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
)
from rich.prompt import Confirm

from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent))
from stats import collect_stats, dir_matches, file_matches, fmt_size, latest_revisions_filter, parse_size, resolve_root  # noqa: E402

BASE_URL = "https://myrient.erista.me/files/"
OUTPUT_ROOT = Path("files")

DEFAULT_WORKERS = 4
MAX_ATTEMPTS = 5       # total attempts per file before giving up (includes re-queues)
CHUNK_SIZE = 1 * 1024 * 1024  # 1 MiB

console = Console()

# Exceptions that indicate a stall rather than a hard failure
_STALL_EXCEPTIONS = (
    requests.Timeout,
    requests.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)


class _Stalled(Exception):
    """Raised when a download stalls so the caller can re-queue it."""


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------

def run_crawl(root: Path) -> None:
    """Run crawl.py against root to ensure all metadata.tsv files exist."""
    console.print(f"[bold cyan]Ensuring metadata is up to date:[/] {root.resolve()}\n")
    script = Path(__file__).parent / "crawl.py"
    subprocess.run(
        ["uv", "run", str(script), "--paths", str(root)],
        check=False,
    )


# ---------------------------------------------------------------------------
# Download list
# ---------------------------------------------------------------------------

def collect_downloads(root: Path, finclude: list[str] = [], fexclude: list[str] = [], dinclude: list[str] = [], dexclude: list[str] = [], latest_revisions: bool = False) -> list[tuple[str, Path, int]]:
    """Return (url, local_path, size_bytes) triples for every file entry under root."""
    items: list[tuple[str, Path, int]] = []
    for tsv_path in sorted(root.rglob("metadata.tsv")):
        rel_dir = "/".join(tsv_path.parent.relative_to(root).parts)
        if rel_dir and not dir_matches(rel_dir, dinclude, dexclude):
            continue
        dir_path = tsv_path.parent
        with open(tsv_path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh, delimiter="\t"))

        file_rows = [r for r in rows if r.get("File Size", "-") != "-"]
        file_rows = [r for r in file_rows if file_matches(r["File Name"], finclude, fexclude)]
        if latest_revisions:
            keep = latest_revisions_filter([r["File Name"] for r in file_rows])
            file_rows = [r for r in file_rows if r["File Name"] in keep]

        for row in file_rows:
            items.append((row["URL"], dir_path / row["File Name"], parse_size(row["File Size"])))
    return items


# ---------------------------------------------------------------------------
# Single-file downloader
# ---------------------------------------------------------------------------

def download_file(
    url: str,
    dest: Path,
    progress: Progress,
    task_id: TaskID,
) -> bool:
    """
    Single download attempt for url → dest (.part while in progress).
    Returns True on success, False on a hard HTTP error.
    Raises _Stalled on timeout / connection drop so the caller can re-queue.
    """
    part = Path(str(dest) + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; MyrientDownloader/1.0)"

    resume_from = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}

    try:
        resp = session.get(url, headers=headers, stream=True, timeout=60)

        if resp.status_code == 416:
            # Server says we already have the whole file
            part.rename(dest)
            return True

        resp.raise_for_status()

        content_length = int(resp.headers.get("Content-Length", 0))
        total = (resume_from + content_length) if content_length else None
        progress.update(task_id, total=total, completed=resume_from)

        with open(part, "ab" if resume_from else "wb") as fh:
            for chunk in resp.iter_content(CHUNK_SIZE):
                if chunk:
                    fh.write(chunk)
                    progress.advance(task_id, len(chunk))

        part.rename(dest)
        return True

    except _STALL_EXCEPTIONS as exc:
        raise _Stalled(str(exc)) from exc
    except requests.HTTPError:
        return False
    except Exception as exc:
        # Treat unexpected errors as stalls — safer to retry than to discard
        raise _Stalled(str(exc)) from exc


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _derive_myrient_url(raw: str, root: Path) -> str:
    if raw.startswith("http"):
        return raw if raw.endswith("/") else raw + "/"
    try:
        rel = root.resolve().relative_to(OUTPUT_ROOT.resolve())
        return BASE_URL + "/".join(quote(p, safe="") for p in rel.parts) + "/"
    except ValueError:
        return BASE_URL


def main() -> None:
    parser = argparse.ArgumentParser(description="Myrient file downloader")
    parser.add_argument(
        "paths",
        nargs="+",
        metavar="PATH_OR_URL",
        help="local files/ paths (glob patterns supported) or Myrient URLs to download",
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS, metavar="N",
        help=f"number of concurrent download threads (default {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--finclude", nargs="+", metavar="PATTERN", default=[],
        help="only download files whose name matches any of these regexes",
    )
    parser.add_argument(
        "--fexclude", nargs="+", metavar="PATTERN", default=[],
        help="exclude files whose name matches any of these regexes (applied after --finclude)",
    )
    parser.add_argument(
        "--dinclude", nargs="+", metavar="PATTERN", default=[],
        help="only download files in directories whose path (relative to root) matches any of these regexes",
    )
    parser.add_argument(
        "--dexclude", nargs="+", metavar="PATTERN", default=[],
        help="exclude directories whose path (relative to root) matches any of these regexes (applied after --dinclude)",
    )
    parser.add_argument(
        "--latest-revisions",
        action="store_true",
        help="for each mainline title, download only the highest (Rev N); "
             "if no revision alternatives exist the base version is downloaded. "
             "Applied after --finclude/--fexclude. Does not affect Beta/Proto/Demo/Sample entries.",
    )
    parser.add_argument(
        "--select",
        action="store_true",
        help="open an interactive TUI to select which files to download (requires textual)",
    )
    args = parser.parse_args()
    n_workers = max(1, min(32, args.workers))

    # Expand glob patterns for local paths; URLs pass through unchanged
    raw_inputs: list[str] = []
    for p in args.paths:
        if p.startswith("http"):
            raw_inputs.append(p)
        else:
            matches = sorted(glob_mod.glob(p))
            raw_inputs.extend(matches if matches else [p])

    # Build (raw, root, myrient_url) triples
    targets = [
        (raw, resolve_root(raw), _derive_myrient_url(raw, resolve_root(raw)))
        for raw in raw_inputs
    ]

    # --- Step 1: Ensure metadata is up to date for all targets ---
    for _raw, root, _url in targets:
        run_crawl(root)

    # Validate existence after crawl
    for _raw, root, _url in targets:
        if not root.exists():
            console.print(f"[red]Error: path does not exist after crawl: {root}[/]")
            sys.exit(1)

    # --- Step 2: Collect downloads and compute pending/done per target ---
    filtering = bool(args.finclude or args.fexclude or args.dinclude or args.dexclude or args.latest_revisions)
    target_stats = []
    for _raw, root, myrient_url in targets:
        file_count, dir_count, total_bytes, max_depth = collect_stats(root, args.finclude, args.fexclude, args.dinclude, args.dexclude, args.latest_revisions)
        if filtering:
            total_file_count, _, total_bytes_all, _ = collect_stats(root)
        else:
            total_file_count, total_bytes_all = file_count, total_bytes
        downloads = collect_downloads(root, args.finclude, args.fexclude, args.dinclude, args.dexclude, args.latest_revisions)
        pending_items   = [(url, path, sz) for url, path, sz in downloads if not path.exists()]
        done_count      = len(downloads) - len(pending_items)
        pending_bytes   = sum(sz for _, _, sz in pending_items)
        target_stats.append((root, myrient_url, file_count, total_file_count, dir_count,
                             total_bytes, total_bytes_all, max_depth,
                             downloads, pending_items, done_count, pending_bytes))

    # --- Step 2b (optional): Interactive file selection ---
    if args.select:
        from utils.download_select import run_selector
        # Merge all downloads across targets for selection
        all_filtered: list[tuple[str, Path, int]] = []
        merged_root = targets[0][1]  # use first target root for relative paths
        for _, _, _, _, _, _, _, _, downloads, _, _, _ in target_stats:
            all_filtered.extend(downloads)
        selected = run_selector(all_filtered, merged_root)
        if selected is None:
            console.print("  Aborted.")
            sys.exit(0)
        if not selected:
            console.print("[bold green]  No files selected — nothing to do![/]")
            sys.exit(0)
        # Rebuild target_stats with only the selected files
        selected_set = {(u, str(p)) for u, p, _ in selected}
        new_target_stats = []
        for (root, myrient_url, _fc, total_file_count, dir_count,
             _tb, total_bytes_all, max_depth,
             downloads, _pi, _dc, _pb) in target_stats:
            downloads = [(u, p, s) for u, p, s in downloads if (u, str(p)) in selected_set]
            file_count = len(downloads)
            total_bytes = sum(s for _, _, s in downloads)
            pending_items = [(u, p, s) for u, p, s in downloads if not p.exists()]
            done_count = file_count - len(pending_items)
            pending_bytes = sum(s for _, _, s in pending_items)
            new_target_stats.append((root, myrient_url, file_count, total_file_count, dir_count,
                                     total_bytes, total_bytes_all, max_depth,
                                     downloads, pending_items, done_count, pending_bytes))
        target_stats = new_target_stats

    console.print()
    console.rule("[bold]Download Overview")

    grand_files = grand_total_files = grand_pending = grand_pending_bytes = grand_total_bytes = 0
    for i, (root, myrient_url, file_count, total_file_count, dir_count,
            total_bytes, total_bytes_all, max_depth,
            _dl, pending_items, done_count, pending_bytes) in enumerate(target_stats, 1):
        if len(target_stats) > 1:
            console.print(f"\n  [bold cyan][{i} / {len(target_stats)}][/]")
        console.print(f"  [dim]Local path  :[/] {root.resolve()}")
        console.print(f"  [dim]Myrient URL :[/] {myrient_url}")
        console.print(f"  [dim]Directories :[/] {dir_count:,}")
        console.print(f"  [dim]Max depth   :[/] {max_depth}")
        if file_count != total_file_count:
            console.print(f"  [dim]Total files :[/] {total_file_count:,}")
        if total_bytes != total_bytes_all:
            console.print(f"  [dim]Total size  :[/] {fmt_size(total_bytes)} selected ({total_bytes:,} bytes)  /  {fmt_size(total_bytes_all)} total ({total_bytes_all:,} bytes)")
        if done_count:
            console.print(f"  [dim]Already done:[/] [green]{done_count:,}[/] / {file_count:,} {'selected ' if file_count != total_file_count else ''}files")
        console.print(f"  [dim]To download :[/] [cyan]{len(pending_items):,}[/] files  ({fmt_size(pending_bytes)})")
        grand_files        += file_count
        grand_total_files  += total_file_count
        grand_pending      += len(pending_items)
        grand_pending_bytes += pending_bytes
        grand_total_bytes  += total_bytes_all

    console.print()
    if len(target_stats) > 1:
        if grand_pending != grand_total_files:
            console.print(f"  [bold]Grand total :[/] {grand_pending:,} to download  /  {grand_total_files:,} total files")
            console.print(f"  [bold]Grand size  :[/] {fmt_size(grand_pending_bytes)} to download ({grand_pending_bytes:,} bytes)  /  {fmt_size(grand_total_bytes)} total ({grand_total_bytes:,} bytes)")
        else:
            console.print(f"  [bold]Grand total :[/] {grand_total_files:,} files to download")
            console.print(f"  [bold]Grand size  :[/] {fmt_size(grand_total_bytes)} ({grand_total_bytes:,} bytes)")
        console.print()

    if grand_pending == 0:
        console.print(f"[bold green]  All {grand_total_files:,} files already downloaded — nothing to do![/]")
        sys.exit(0)

    if not Confirm.ask(
        f"  Download [bold cyan]{grand_pending:,}[/] files ([bold]{fmt_size(grand_pending_bytes)}[/])?"
    ):
        console.print("  Aborted.")
        sys.exit(0)

    # --- Step 3: Flatten pending list across all targets ---
    all_downloads: list[tuple[str, Path, int]] = []
    for _, _, _, _, _, _, _, _, downloads, _, _, _ in target_stats:
        all_downloads.extend(downloads)

    pending = [(url, path, sz) for url, path, sz in all_downloads if not path.exists()]
    already_done = len(all_downloads) - len(pending)

    console.print()

    if not pending:
        console.print("[bold green]  Nothing to download — all files already present![/]")
        sys.exit(0)

    # --- Step 4: Download with rich progress and re-queue on stall ---
    completed = already_done
    failed = 0
    lock = threading.Lock()

    # Queue items: (url, local_path, attempt_number)
    work_q: queue.Queue = queue.Queue()
    for url, path, _sz in pending:
        work_q.put((url, path, 1))

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.fields[filename]}", justify="left"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:

        def worker() -> None:
            nonlocal completed, failed
            while True:
                item = work_q.get()
                if item is None:
                    work_q.task_done()
                    break
                url, dest, attempt = item
                task_id = progress.add_task("file", filename=dest.name, total=None, start=True)
                try:
                    ok: bool | None = download_file(url, dest, progress, task_id)
                except _Stalled:
                    ok = None  # will re-queue if attempts remain
                progress.remove_task(task_id)

                with lock:
                    if ok is True:
                        completed += 1
                        progress.console.print(f"  [green]✓[/] {dest.name}")
                    elif ok is None and attempt < MAX_ATTEMPTS:
                        # Stalled — push back onto the end of the queue
                        work_q.put((url, dest, attempt + 1))
                        progress.console.print(
                            f"  [yellow]↺ Stalled, re-queued[/] {dest.name}"
                            f"  [dim](attempt {attempt + 1}/{MAX_ATTEMPTS})[/]"
                        )
                    else:
                        failed += 1
                        label = f"gave up after {MAX_ATTEMPTS} stalls" if ok is None else "hard error"
                        progress.console.print(f"  [red]✗ {label}:[/] {dest.name}")
                    progress.update(
                        overall_task,
                        completed=completed + failed,
                        filename=f"[cyan]Overall[/]  {completed + failed}/{len(all_downloads)} files",
                    )
                work_q.task_done()

        threads = [
            threading.Thread(target=worker, name=f"dl-{i+1}", daemon=True)
            for i in range(n_workers)
        ]
        for t in threads:
            t.start()
        # Add overall last so it renders below the active file bars
        overall_task = progress.add_task(
            "overall",
            filename=f"[cyan]Overall[/]  {already_done}/{len(all_downloads)} files",
            total=len(all_downloads),
            completed=already_done,
        )
        work_q.join()
        for _ in threads:
            work_q.put(None)
        for t in threads:
            t.join()

    console.print()
    if failed:
        console.print(f"[yellow]  Done with {failed:,} failure(s). Re-run to retry.[/]")
    else:
        console.print(f"[bold green]  All {completed:,} files downloaded successfully![/]")


if __name__ == "__main__":
    main()
