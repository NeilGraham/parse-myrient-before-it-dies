#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "beautifulsoup4",
#   "requests",
#   "rich",
# ]
# ///
"""
Myrient downloader.
Ensures metadata is up to date via crawl.py, shows a stats overview,
confirms with the user, then downloads all files under the given path.

Resume-safe: skips complete files, resumes .part files via HTTP Range requests.
"""

import argparse
import csv
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import Confirm

sys.path.insert(0, str(Path(__file__).parent))
from stats import collect_stats, fmt_size, resolve_root  # noqa: E402

DEFAULT_WORKERS = 4
CHUNK_SIZE = 1 * 1024 * 1024  # 1 MiB
MAX_RETRIES = 3

console = Console()


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------

def run_crawl(root: Path) -> None:
    """Run crawl.py against root to ensure all metadata.tsv files exist."""
    console.print(f"[bold cyan]Ensuring metadata is up to date:[/] {root.resolve()}\n")
    script = Path(__file__).parent / "crawl.py"
    subprocess.run(
        [sys.executable, str(script), "--paths", str(root)],
        check=False,
    )


# ---------------------------------------------------------------------------
# Download list
# ---------------------------------------------------------------------------

def collect_downloads(root: Path) -> list[tuple[str, Path]]:
    """Return (url, local_path) pairs for every file entry under root."""
    items: list[tuple[str, Path]] = []
    for tsv_path in sorted(root.rglob("metadata.tsv")):
        dir_path = tsv_path.parent
        with open(tsv_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                if row.get("File Size", "-") != "-":
                    items.append((row["URL"], dir_path / row["File Name"]))
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
    """Download url → dest, using dest.part while in progress. Return True on success."""
    part = Path(str(dest) + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; MyrientDownloader/1.0)"

    for attempt in range(1, MAX_RETRIES + 1):
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

        except Exception as exc:
            if attempt == MAX_RETRIES:
                progress.console.print(
                    f"  [red]✗ Failed after {MAX_RETRIES} attempts:[/] {dest.name} — {exc}"
                )
                return False

    return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Myrient file downloader")
    parser.add_argument(
        "path",
        metavar="PATH_OR_URL",
        help="local files/ path or Myrient URL to download",
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS, metavar="N",
        help=f"number of concurrent download threads (default {DEFAULT_WORKERS})",
    )
    args = parser.parse_args()
    n_workers = max(1, min(32, args.workers))

    root = resolve_root(args.path)

    # --- Step 1: Ensure metadata is up to date ---
    run_crawl(root)

    if not root.exists():
        console.print(f"[red]Error: path does not exist after crawl: {root}[/]")
        sys.exit(1)

    # --- Step 2: Stats overview and confirmation ---
    file_count, dir_count, total_bytes, max_depth = collect_stats(root)

    console.print()
    console.rule("[bold]Download Overview")
    console.print(f"  [dim]Root        :[/] {root.resolve()}")
    console.print(f"  [dim]Files       :[/] {file_count:,}")
    console.print(f"  [dim]Directories :[/] {dir_count:,}")
    console.print(f"  [dim]Max depth   :[/] {max_depth}")
    console.print(f"  [dim]Total size  :[/] {fmt_size(total_bytes)}  ({total_bytes:,} bytes)")
    console.print()

    if not Confirm.ask(
        f"  Download all [bold cyan]{file_count:,}[/] files ([bold]{fmt_size(total_bytes)}[/])?"
    ):
        console.print("  Aborted.")
        sys.exit(0)

    # --- Step 3: Determine what still needs downloading ---
    all_downloads = collect_downloads(root)
    pending = [(url, path) for url, path in all_downloads if not path.exists()]
    already_done = len(all_downloads) - len(pending)

    console.print()
    console.print(
        f"  [green]{already_done:,}[/] files already downloaded, "
        f"[cyan]{len(pending):,}[/] remaining."
    )
    console.print()

    if not pending:
        console.print("[bold green]  Nothing to download — all files already present![/]")
        sys.exit(0)

    # --- Step 4: Download with rich progress ---
    completed = already_done
    failed = 0
    lock = threading.Lock()

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.fields[filename]}", justify="left"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        overall_task = progress.add_task(
            "overall",
            filename=f"[cyan]Overall[/]  {already_done}/{len(all_downloads)} files",
            total=len(all_downloads),
            completed=already_done,
        )

        def do_download(url: str, dest: Path) -> bool:
            nonlocal completed, failed
            task_id = progress.add_task("file", filename=dest.name, total=None, start=True)
            ok = download_file(url, dest, progress, task_id)
            progress.remove_task(task_id)
            with lock:
                if ok:
                    completed += 1
                    progress.console.print(f"  [green]✓[/] {dest.name}")
                else:
                    failed += 1
                    progress.console.print(f"  [red]✗[/] {dest.name}")
                progress.update(
                    overall_task,
                    completed=completed,
                    filename=f"[cyan]Overall[/]  {completed}/{len(all_downloads)} files",
                )
            return ok

        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            futures = {executor.submit(do_download, url, path): path for url, path in pending}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    path = futures[future]
                    progress.console.print(f"  [red]Unexpected error for {path.name}:[/] {exc}")

    console.print()
    if failed:
        console.print(f"[yellow]  Done with {failed:,} failure(s). Re-run to retry.[/]")
    else:
        console.print(f"[bold green]  All {completed:,} files downloaded successfully![/]")


if __name__ == "__main__":
    main()
