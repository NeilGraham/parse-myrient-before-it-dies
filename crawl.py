#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "beautifulsoup4",
#   "requests",
# ]
# ///
"""
Myrient directory crawler.
Recursively walks https://myrient.erista.me/files/ and writes a metadata.tsv
(File Name, File Size, Date, URL) for every directory it visits.

Output mirrors the URL structure under ./files/
Resume-safe: skips any directory that already has a metadata.tsv.
"""

import argparse
import csv
import queue
import sys
import threading
import time
import logging
import tempfile
from pathlib import Path
from urllib.parse import urljoin, unquote

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://myrient.erista.me/files/"
OUTPUT_ROOT = Path("files")
DEFAULT_DELAY = 1.0
DEFAULT_WORKERS = 8
MAX_RETRIES = 3
FIELDNAMES = ["File Name", "File Size", "Date", "URL"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(threadName)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("crawl.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thread-local requests session
# ---------------------------------------------------------------------------

_thread_local = threading.local()


def get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers["User-Agent"] = "Mozilla/5.0 (compatible; MyrientCrawler/1.0)"
        _thread_local.session = s
    return _thread_local.session


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

SKIP_HREFS = {"./", "../"}
SKIP_TITLES = {".", ".."}


def parse_directory(html: str, page_url: str) -> list[dict]:
    """Return list of row dicts for a directory listing page."""
    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.find("tbody")
    if not tbody:
        log.warning("No <tbody> found at %s", page_url)
        return []

    rows = []
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue

        link = tds[0].find("a")
        if not link:
            continue

        href = link.get("href", "")
        title = link.get("title", "")

        # Skip Parent Directory, ./, ../
        if href in SKIP_HREFS or title in SKIP_TITLES:
            continue
        # Also catch the un-titled "Parent directory/" entry
        if not title and href == "../":
            continue

        file_name = title if title else link.get_text(strip=True)
        file_size = tds[1].get_text(strip=True)
        date = tds[2].get_text(strip=True)
        full_url = urljoin(page_url, href)

        rows.append({
            "File Name": file_name,
            "File Size": file_size,
            "Date": date,
            "URL": full_url,
        })

    return rows


# ---------------------------------------------------------------------------
# Fetching with retry
# ---------------------------------------------------------------------------

def fetch_page(url: str, delay: float) -> str | None:
    session = get_session()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            time.sleep(delay)
            return resp.text
        except Exception as exc:
            log.warning("Attempt %d/%d failed for %s: %s", attempt, MAX_RETRIES, url, exc)
            time.sleep(delay * attempt)
    log.error("Giving up on %s after %d attempts", url, MAX_RETRIES)
    return None


# ---------------------------------------------------------------------------
# TSV writing
# ---------------------------------------------------------------------------

def write_tsv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with open(tmp_fd, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter="\t")
            writer.writeheader()
            writer.writerows(rows)
        Path(tmp_path).replace(path)
    except:
        Path(tmp_path).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# BFS crawl
# ---------------------------------------------------------------------------

_visited: set[str] = set()
_visited_lock = threading.Lock()


def mark_visited(url: str) -> bool:
    """Return True if url is newly marked; False if already seen."""
    with _visited_lock:
        if url in _visited:
            return False
        _visited.add(url)
        return True


def enqueue_subdirs(rows: list[dict], out_dir: Path, work_queue: queue.Queue) -> None:
    for row in rows:
        if row["File Size"] == "-":
            sub_name = unquote(row["URL"].rstrip("/").split("/")[-1])
            safe_name = sub_name.replace("\\", "_").replace(":", "_")
            work_queue.put((row["URL"], out_dir / safe_name))


def read_tsv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def crawl_one(url: str, out_dir: Path, work_queue: queue.Queue, delay: float) -> None:
    tsv_path = out_dir / "metadata.tsv"

    if tsv_path.exists():
        log.info("SKIP (already done)  %s", url)
        enqueue_subdirs(read_tsv(tsv_path), out_dir, work_queue)
        return

    if not mark_visited(url):
        return

    log.info("CRAWL  %s", url)
    html = fetch_page(url, delay)
    if html is None:
        return

    rows = parse_directory(html, url)
    write_tsv(tsv_path, rows)
    log.info("  -> %d entries written to %s", len(rows), tsv_path)

    enqueue_subdirs(rows, out_dir, work_queue)


def worker(work_queue: queue.Queue, delay: float) -> None:
    while True:
        item = work_queue.get()
        if item is None:
            work_queue.task_done()
            break
        try:
            crawl_one(*item, work_queue=work_queue, delay=delay)
        finally:
            work_queue.task_done()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Myrient directory crawler")
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS, metavar="N",
        help=f"number of worker threads (1–32, default {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY, metavar="SEC",
        help=f"polite delay between requests per thread (default {DEFAULT_DELAY}s)",
    )
    args = parser.parse_args()
    n_workers = max(1, min(32, args.workers))
    delay = max(0.0, args.delay)

    log.info(
        "Starting Myrient crawler. workers=%d delay=%.1fs output=%s",
        n_workers, delay, OUTPUT_ROOT.resolve(),
    )

    work_queue: queue.Queue = queue.Queue()
    work_queue.put((BASE_URL, OUTPUT_ROOT))

    threads = [
        threading.Thread(target=worker, args=(work_queue, delay), name=f"worker-{i+1}", daemon=True)
        for i in range(n_workers)
    ]
    for t in threads:
        t.start()

    work_queue.join()

    # Send poison pills to shut down worker threads
    for _ in threads:
        work_queue.put(None)
    for t in threads:
        t.join()

    log.info("Done.")


if __name__ == "__main__":
    main()
