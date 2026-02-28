from __future__ import annotations
"""
fetch_episodes.py

Fetches RSS feeds from Soundon, finds episodes published within the
configured lookback window, and downloads the audio files.

Output structure:
  {parent_folder}/audio/{program_name}/{program_name}_{YYYYMMDD}.ext

Each speaker has a persistent folder. Downloads are skipped if the file
already exists (checked by filename / date), so re-runs are safe.
"""

import os
import sys
import yaml
import feedparser
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False


# ---------------------------------------------------------------------------
# Config & date helpers
# ---------------------------------------------------------------------------

def load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_date_range(lookback_days: int):
    """Return (start_date, end_date) as date objects in UTC."""
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=lookback_days)
    return start, today


def folder_name_for_range(start_date, end_date) -> str:
    return f"{start_date.strftime('%Y%m%d')}-{end_date.strftime('%Y%m%d')}"


# ---------------------------------------------------------------------------
# RSS parsing helpers
# ---------------------------------------------------------------------------

def parse_pub_date(entry) -> "date | None":
    """Extract publication date from a feedparser entry, normalised to UTC date."""
    raw = entry.get("published") or entry.get("updated")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        # Convert to UTC if timezone-aware
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return dt.date()
    except Exception:
        return None


def extract_audio_url(entry) -> "str | None":
    """Pull the first audio enclosure URL from a feedparser entry."""
    # feedparser exposes enclosures as a list
    for enc in entry.get("enclosures", []):
        if "audio" in enc.get("type", ""):
            return enc.get("href") or enc.get("url")

    # Fallback: iterate links
    for link in entry.get("links", []):
        if link.get("rel") == "enclosure":
            url = link.get("href", "")
            mime = link.get("type", "")
            if "audio" in mime or url.lower().endswith((".mp3", ".m4a", ".ogg", ".aac")):
                return url

    return None


def url_extension(url: str) -> str:
    """Return file extension from URL path, defaulting to .mp3."""
    path = urlparse(url).path
    ext = os.path.splitext(path)[1]
    return ext if ext else ".mp3"


# ---------------------------------------------------------------------------
# Download helper
# ---------------------------------------------------------------------------

def download_file(url: str, dest: Path) -> None:
    """Stream-download *url* into *dest*, with an optional tqdm progress bar."""
    response = requests.get(url, stream=True, timeout=60)
    response.raise_for_status()

    total = int(response.headers.get("content-length", 0))
    chunk_size = 8192

    if HAS_TQDM and total:
        bar = tqdm(total=total, unit="B", unit_scale=True, desc=dest.name, leave=False)
    else:
        bar = None

    with open(dest, "wb") as fh:
        for chunk in response.iter_content(chunk_size=chunk_size):
            fh.write(chunk)
            if bar:
                bar.update(len(chunk))

    if bar:
        bar.close()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def fetch_and_download(config: dict, folder_name: str | None = None) -> None:
    """Fetch RSS feeds and download new episodes into the run folder.

    Parameters
    ----------
    config:      Parsed config dict.
    folder_name: Pre-computed run folder (e.g. '20260218-20260225').
                 If None, it is derived from config['lookback_days'].
    """
    lookback_days = int(config.get("lookback_days", 7))
    start_date, end_date = get_date_range(lookback_days)

    if folder_name is None:
        run_folder = folder_name_for_range(start_date, end_date)
    else:
        run_folder = folder_name
        # Parse dates back from the folder name for the range check
        parts = folder_name.split("-")
        from datetime import date
        start_date = date(int(parts[0][:4]), int(parts[0][4:6]), int(parts[0][6:8]))
        end_date   = date(int(parts[1][:4]), int(parts[1][4:6]), int(parts[1][6:8]))

    parent_folder = Path(config["parent_folder"])
    audio_root = parent_folder / "audio"

    print(f"Date range  : {start_date} → {end_date}")
    print(f"Audio root  : {audio_root}")
    print()

    total_downloaded = 0

    for feed_cfg in config.get("feeds", []):
        program_name = feed_cfg["name"]
        feed_url = feed_cfg["url"]

        speaker_dir = audio_root / program_name
        speaker_dir.mkdir(parents=True, exist_ok=True)

        print(f"[{program_name}] Fetching feed …")
        parsed = feedparser.parse(feed_url)

        if parsed.bozo and not parsed.entries:
            print(f"  WARNING: could not parse feed ({feed_url})")
            continue

        found = 0
        for entry in parsed.entries:
            pub_date = parse_pub_date(entry)
            if pub_date is None:
                continue
            if not (start_date <= pub_date <= end_date):
                continue

            audio_url = extract_audio_url(entry)
            if not audio_url:
                print(f"  SKIP (no audio enclosure): {entry.get('title', '—')}")
                continue

            ext = url_extension(audio_url)
            date_str = pub_date.strftime("%Y%m%d")
            filename = f"{program_name}_{date_str}{ext}"
            dest = speaker_dir / filename

            print(f"  Downloading: {filename}")
            try:
                download_file(audio_url, dest)
                print(f"  Saved      : {dest}")
                found += 1
                total_downloaded += 1
            except Exception as exc:
                print(f"  ERROR downloading {filename}: {exc}")
                # Remove partial file if it exists
                if dest.exists():
                    dest.unlink()

        if found == 0:
            print(f"  No new episodes in the past {lookback_days} days.")
        print()

    print(f"Done. {total_downloaded} file(s) newly downloaded → {audio_root}")


def run(config_path: str = "config.yaml") -> None:
    config = load_config(config_path)
    fetch_and_download(config)


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    run(config_path)
