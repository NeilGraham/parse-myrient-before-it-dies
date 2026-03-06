#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "selenium",
#   "webdriver-manager",
#   "beautifulsoup4",
# ]
# ///
"""
Myrient directory crawler.
Recursively walks https://myrient.erista.me/files/ and writes a metadata.tsv
(File Name, File Size, Date, URL) for every directory it visits.

Output mirrors the URL structure under ./files/
Resume-safe: skips any directory that already has a metadata.tsv.
"""

import csv
import sys
import time
import logging
import tempfile
from pathlib import Path
from urllib.parse import urljoin, unquote

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

BASE_URL = "https://myrient.erista.me/files/"
OUTPUT_ROOT = Path("files")
DELAY_SECONDS = 1.0          # polite crawl delay between requests
MAX_RETRIES = 3
FIELDNAMES = ["File Name", "File Size", "Date", "URL"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("crawl.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Selenium driver
# ---------------------------------------------------------------------------

def make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (compatible; MyrientCrawler/1.0)"
    )
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


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

def fetch_page(driver: webdriver.Chrome, url: str) -> str | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            driver.get(url)
            time.sleep(DELAY_SECONDS)
            return driver.page_source
        except Exception as exc:
            log.warning("Attempt %d/%d failed for %s: %s", attempt, MAX_RETRIES, url, exc)
            time.sleep(DELAY_SECONDS * attempt)
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
# Recursive crawl
# ---------------------------------------------------------------------------

def crawl(driver: webdriver.Chrome, url: str, out_dir: Path) -> None:
    tsv_path = out_dir / "metadata.tsv"

    if tsv_path.exists():
        log.info("SKIP (already done)  %s", url)
        return

    log.info("CRAWL  %s", url)
    html = fetch_page(driver, url)
    if html is None:
        return

    rows = parse_directory(html, url)
    write_tsv(tsv_path, rows)
    log.info("  -> %d entries written to %s", len(rows), tsv_path)

    # Recurse into subdirectories (File Size == '-' means it's a folder)
    for row in rows:
        if row["File Size"] == "-":
            sub_name = unquote(row["URL"].rstrip("/").split("/")[-1])
            # Sanitize name for use as a filesystem path component
            safe_name = sub_name.replace("\\", "_").replace(":", "_")
            crawl(driver, row["URL"], out_dir / safe_name)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Starting Myrient crawler. Output root: %s", OUTPUT_ROOT.resolve())
    driver = make_driver()
    try:
        crawl(driver, BASE_URL, OUTPUT_ROOT)
    finally:
        driver.quit()
    log.info("Done.")


if __name__ == "__main__":
    main()
