from __future__ import annotations
"""
upload_to_notebooklm.py

Creates a fresh NotebookLM notebook for a weekly run and uploads all
transcript .txt files as individual sources.

A fresh notebook is always created — if a notebook with the same title
already exists it is deleted first, so stale cached content never bleeds
into a new run.

Requires:
  pip install notebooklm-mcp-cli
  nlm login          # one-time browser auth

Usage:
  python upload_to_notebooklm.py                        # auto-detect latest run folder
  python upload_to_notebooklm.py --folder 20260218-20260225
  python upload_to_notebooklm.py --config my_config.yaml --folder 20260218-20260225
"""

import argparse
import json
import subprocess
import sys
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path

# A real podcast transcript should comfortably exceed this.
# This is intentionally stricter than transcribe.py's MIN_TRANSCRIPT_CHARS (50),
# which only guards against whisper crashes. Here we guard against uploading
# near-empty files that would pollute the NotebookLM notebook.
MIN_TRANSCRIPT_UPLOAD_CHARS = 500


# ---------------------------------------------------------------------------
# Config & helpers
# ---------------------------------------------------------------------------

def load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def default_folder_name(lookback_days: int) -> str:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=lookback_days)
    return f"{start.strftime('%Y%m%d')}-{today.strftime('%Y%m%d')}"


def find_transcripts_for_run(transcript_root: Path, folder_name: str) -> list[Path]:
    """Collect transcript files across per-speaker subdirs whose date falls in the run window."""
    parts = folder_name.split("-")
    start_str, end_str = parts[0], parts[1]

    files = []
    if not transcript_root.exists():
        return files
    for speaker_dir in sorted(transcript_root.iterdir()):
        if not speaker_dir.is_dir():
            continue
        for f in speaker_dir.glob("*.txt"):
            date_str = f.stem.split("_")[-1]
            if len(date_str) == 8 and start_str <= date_str <= end_str:
                files.append(f)
    return sorted(files)


# ---------------------------------------------------------------------------
# nlm CLI wrappers
# ---------------------------------------------------------------------------

def _run_nlm(nlm_path: str, *args: str, capture: bool = True) -> subprocess.CompletedProcess:
    """Run an nlm command and return the CompletedProcess result."""
    cmd = [nlm_path, *args]
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=capture, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip() if result.stderr else ""
        raise RuntimeError(
            f"nlm command failed (exit {result.returncode}): {stderr or '(no stderr)'}"
        )
    return result


def check_nlm_auth(nlm_path: str) -> None:
    """Verify that nlm is installed and the user is authenticated."""
    try:
        _run_nlm(nlm_path, "login", "--check")
    except FileNotFoundError:
        print(
            f"ERROR: `nlm` command not found at '{nlm_path}'.\n"
            "Install it with:  pip install notebooklm-mcp-cli\n"
            "Then authenticate: nlm login\n"
            "Set nlm_path in config.yaml to the full path of the nlm binary."
        )
        sys.exit(1)
    except RuntimeError as exc:
        print(
            f"ERROR: NotebookLM authentication check failed.\n{exc}\n"
            "Run `nlm login` to authenticate."
        )
        sys.exit(1)


def list_notebooks(nlm_path: str) -> list[dict]:
    """Return all notebooks as a list of dicts (id, title, …)."""
    try:
        result = _run_nlm(nlm_path, "notebook", "list", "--json")
        data = json.loads(result.stdout)
        # nlm may return a top-level list or {"notebooks": [...]}
        if isinstance(data, list):
            return data
        return data.get("notebooks", [])
    except Exception as exc:
        print(f"  WARNING: could not list notebooks: {exc}")
        return []


def find_notebook_by_title(nlm_path: str, title: str) -> "str | None":
    """Return the ID of the first notebook whose title matches, or None."""
    for nb in list_notebooks(nlm_path):
        nb_title = nb.get("title") or nb.get("name") or ""
        if nb_title.strip() == title.strip():
            return nb.get("notebook_id") or nb.get("id")
    return None


def delete_notebook(nlm_path: str, notebook_id: str) -> None:
    """Delete a notebook by ID (non-fatal on failure)."""
    try:
        _run_nlm(nlm_path, "notebook", "delete", notebook_id)
        print(f"  Deleted stale notebook: {notebook_id}")
    except RuntimeError as exc:
        print(f"  WARNING: could not delete notebook {notebook_id}: {exc}")


def create_notebook(nlm_path: str, title: str) -> str:
    """Create a new NotebookLM notebook and return its ID."""
    # `nlm notebook create` no longer supports --json, so create then look up by title.
    _run_nlm(nlm_path, "notebook", "create", title)
    notebook_id = find_notebook_by_title(nlm_path, title)
    if not notebook_id:
        raise RuntimeError(f"Notebook '{title}' was created but could not be found in the list.")
    return notebook_id


def add_source_file(nlm_path: str, notebook_id: str, file_path: Path) -> None:
    """Upload a single transcript file to a NotebookLM notebook."""
    # --wait blocks until NotebookLM finishes processing the source
    _run_nlm(
        nlm_path,
        "source", "add", notebook_id,
        "--file", str(file_path),
        "--wait",
        capture=False,  # stream live output so the user can see progress
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(config: dict, folder_name: str) -> str:
    """Run the upload stage and return the notebook_id."""
    nlm_path = config.get("nlm_path", "nlm")
    parent_folder = Path(config["parent_folder"])
    transcript_root = parent_folder / "transcripts"

    txt_files = find_transcripts_for_run(transcript_root, folder_name)
    if not txt_files:
        print(f"No transcript files found in {transcript_root} for window {folder_name}")
        print("Run transcribe.py first to generate transcripts.")
        sys.exit(1)

    print(f"Transcript root   : {transcript_root}")
    print(f"Files to upload   : {len(txt_files)}")
    print()

    # --- Auth check ---
    print("Checking NotebookLM authentication …")
    check_nlm_auth(nlm_path)
    print("Authenticated.\n")

    # --- Always create a fresh notebook ---
    # Delete any existing notebook with the same title first so stale
    # cached content from a previous run cannot leak into this week's output.
    notebook_prefix = config.get("notebooklm_notebook_prefix", "股市週報")
    notebook_title = f"{notebook_prefix} {folder_name}"

    print(f"Checking for existing notebook titled '{notebook_title}' …")
    stale_id = find_notebook_by_title(nlm_path, notebook_title)
    if stale_id:
        print(f"  Found stale notebook ({stale_id}) — deleting it for a clean slate …")
        delete_notebook(nlm_path, stale_id)
    else:
        print("  None found.")

    print(f"\nCreating fresh notebook: '{notebook_title}' …")
    notebook_id = create_notebook(nlm_path, notebook_title)
    print(f"Notebook created: {notebook_id}\n")

    # --- Transcript sanity check ---
    print("Validating transcript contents …")
    valid_files, skipped_files = [], []
    for txt_file in txt_files:
        char_count = len(txt_file.read_text(encoding="utf-8").strip())
        if char_count < MIN_TRANSCRIPT_UPLOAD_CHARS:
            print(f"  ~ SKIP — suspiciously short ({char_count} chars < {MIN_TRANSCRIPT_UPLOAD_CHARS}): {txt_file.name}")
            skipped_files.append(txt_file.name)
        else:
            print(f"  ✓ OK ({char_count} chars): {txt_file.name}")
            valid_files.append(txt_file)

    if not valid_files:
        print("\nERROR: All transcripts failed the sanity check — nothing to upload.")
        sys.exit(1)

    if skipped_files:
        print(f"\n  WARNING: {len(skipped_files)} transcript(s) skipped, "
              f"{len(valid_files)} will be uploaded.\n")
    else:
        print()

    # --- Upload each transcript ---
    success, failed = 0, []

    for idx, txt_file in enumerate(valid_files, start=1):
        print(f"[{idx}/{len(valid_files)}] Uploading: {txt_file.name}")
        try:
            add_source_file(nlm_path, notebook_id, txt_file)
            success += 1
        except RuntimeError as exc:
            print(f"  ERROR: {exc}")
            failed.append(txt_file.name)
        print()

    # --- Summary ---
    print("=" * 60)
    print(f"Done. {success}/{len(valid_files)} transcript(s) uploaded."
          + (f"  ({len(skipped_files)} skipped — too short)" if skipped_files else ""))
    print(f"Notebook ID  : {notebook_id}")
    print(f"Open in browser: https://notebooklm.google.com/notebook/{notebook_id}")
    if failed:
        print(f"\nFailed uploads ({len(failed)}):")
        for name in failed:
            print(f"  - {name}")

    return notebook_id


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload podcast transcripts to a fresh NotebookLM notebook."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config YAML (default: config.yaml)",
    )
    parser.add_argument(
        "--folder",
        default=None,
        help=(
            "Run folder name, e.g. 20260218-20260225. "
            "Defaults to the current lookback window defined in config."
        ),
    )
    args = parser.parse_args()

    config = load_config(args.config)
    folder_name = args.folder or default_folder_name(int(config.get("lookback_days", 7)))

    print(f"Run folder: {folder_name}\n")
    run(config, folder_name)


if __name__ == "__main__":
    main()
