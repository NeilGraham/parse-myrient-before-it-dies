#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "rich",
# ]
# ///
"""
Myrient file validator.
Recursively scans given paths for .zip and .chd files and validates each one.

  .zip  — validated via Python's zipfile module (CRC checks on all members)
  .chd  — validated via `chdman verify -i <file>` (skipped if chdman is absent)
"""

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

console = Console()

CHDMAN_AVAILABLE = shutil.which("chdman") is not None


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def validate_zip(path: Path) -> tuple[bool, str]:
    """Return (ok, message)."""
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad is not None:
                return False, f"bad member: {bad}"
        return True, "ok"
    except zipfile.BadZipFile as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def validate_chd(path: Path) -> tuple[bool, str]:
    """Return (ok, message). Assumes chdman is available."""
    try:
        result = subprocess.run(
            ["chdman", "verify", "-i", str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True, "ok"
        detail = (result.stderr or result.stdout).strip().splitlines()[-1] if (result.stderr or result.stdout).strip() else "non-zero exit"
        return False, detail
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def collect_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        root = root.resolve()
        if root.is_file():
            if root.suffix.lower() in {".zip", ".chd"}:
                files.append(root)
        elif root.is_dir():
            for ext in ("*.zip", "*.chd"):
                files.extend(sorted(root.rglob(ext)))
        else:
            console.print(f"[yellow]Warning:[/yellow] {root} is not a file or directory, skipping.")
    return files


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate .zip and .chd files under the given paths.",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="Files or directories to validate (directories are searched recursively).",
    )
    parser.add_argument(
        "--no-chd",
        action="store_true",
        help="Skip .chd files even if chdman is available.",
    )
    parser.add_argument(
        "--failures-only",
        action="store_true",
        help="Only print files that fail validation.",
    )
    args = parser.parse_args()

    skip_chd = args.no_chd or not CHDMAN_AVAILABLE
    if not CHDMAN_AVAILABLE and not args.no_chd:
        console.print("[yellow]chdman not found — .chd files will be skipped.[/yellow]")

    roots = [Path(p) for p in args.paths]
    all_files = collect_files(roots)

    if skip_chd:
        files = [f for f in all_files if f.suffix.lower() != ".chd"]
        skipped_chd = [f for f in all_files if f.suffix.lower() == ".chd"]
    else:
        files = all_files
        skipped_chd = []

    if not files:
        console.print("[yellow]No files to validate.[/yellow]")
        return 0

    zip_count = sum(1 for f in files if f.suffix.lower() == ".zip")
    chd_count = sum(1 for f in files if f.suffix.lower() == ".chd")
    console.print(
        f"Validating [bold]{len(files)}[/bold] file(s): "
        f"{zip_count} .zip, {chd_count} .chd"
        + (f" ([dim]{len(skipped_chd)} .chd skipped[/dim])" if skipped_chd else "")
    )

    ok_list: list[Path] = []
    fail_list: list[tuple[Path, str]] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Validating…", total=len(files))

        for path in files:
            progress.update(task, description=f"[cyan]{path.name}[/cyan]")
            ext = path.suffix.lower()
            if ext == ".zip":
                ok, msg = validate_zip(path)
            elif ext == ".chd":
                ok, msg = validate_chd(path)
            else:
                progress.advance(task)
                continue

            if ok:
                ok_list.append(path)
                if not args.failures_only:
                    console.print(f"  [green]OK[/green]  {path}")
            else:
                fail_list.append((path, msg))
                console.print(f"  [red]FAIL[/red] {path}  [dim]{msg}[/dim]")

            progress.advance(task)

    # Summary
    console.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("[green]Passed[/green]", str(len(ok_list)))
    table.add_row("[red]Failed[/red]", str(len(fail_list)))
    if skipped_chd:
        table.add_row("[yellow]Skipped (.chd)[/yellow]", str(len(skipped_chd)))
    console.print(table)

    return 1 if fail_list else 0


if __name__ == "__main__":
    sys.exit(main())
