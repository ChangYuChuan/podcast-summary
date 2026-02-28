from __future__ import annotations
"""
pipeline.py

Master pipeline: fetch → transcribe → upload to NotebookLM → email report.

All four stages share the same config and run folder so output
paths are always consistent.

Usage:
  python pipeline.py                              # full run, auto date range
  python pipeline.py --folder 20260218-20260225   # target a specific week
  python pipeline.py --skip-upload                # fetch + transcribe only
  python pipeline.py --skip-fetch --skip-transcribe  # upload only (re-run)
  python pipeline.py --skip-email                 # skip the email stage
  python pipeline.py --config my_config.yaml

Note: the upload stage always creates a FRESH notebook, deleting any stale
notebook with the same title first. This prevents old cached content from
bleeding into a new week's output.
"""

import argparse
import sys
import time
import traceback
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Minimum acceptable audio file size.
# A real podcast episode should be several MB; anything at or below this
# is almost certainly a failed/partial download and must not be transcribed.
MIN_AUDIO_BYTES = 512 * 1024  # 512 KB

SUPPORTED_AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".aac", ".wav", ".flac", ".opus"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def default_folder_name(lookback_days: int) -> str:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=lookback_days)
    return f"{start.strftime('%Y%m%d')}-{today.strftime('%Y%m%d')}"


def banner(title: str) -> None:
    width = 60
    print()
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def elapsed(start: float) -> str:
    secs = int(time.time() - start)
    m, s = divmod(secs, 60)
    return f"{m}m {s}s" if m else f"{s}s"


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------

def validate_audio_files(config: dict, folder_name: str) -> bool:
    """Check every downloaded audio file is intact before transcription.

    Rules:
      - 0-byte files are deleted immediately (corrupt download) and counted as failures.
      - Files below MIN_AUDIO_BYTES are logged as warnings but kept — a very short
        episode or low-bitrate file may still be legitimate.
      - Returns False (abort) only when zero usable audio files remain after cleanup.
    """
    banner("GUARD — Audio File Integrity")
    audio_root = Path(config["parent_folder"]) / "audio"
    parts = folder_name.split("-")
    start_str, end_str = parts[0], parts[1]

    audio_files = []
    if audio_root.exists():
        for speaker_dir in sorted(audio_root.iterdir()):
            if not speaker_dir.is_dir():
                continue
            for ext in SUPPORTED_AUDIO_EXTS:
                for f in speaker_dir.glob(f"*{ext}"):
                    date_str = f.stem.split("_")[-1]
                    if len(date_str) == 8 and start_str <= date_str <= end_str:
                        audio_files.append(f)
    audio_files = sorted(audio_files)

    if not audio_files:
        print(f"  ERROR: No audio files found in {audio_root}")
        return False

    usable = 0
    for f in audio_files:
        size = f.stat().st_size
        size_mb = size / (1024 * 1024)
        if size == 0:
            print(f"  ✗ CORRUPT — deleting 0-byte file: {f.name}")
            f.unlink()
        elif size < MIN_AUDIO_BYTES:
            print(f"  ~ WARNING — suspiciously small ({size_mb:.2f} MB): {f.name}")
            usable += 1
        else:
            print(f"  ✓ OK ({size_mb:.1f} MB): {f.name}")
            usable += 1

    print()
    if usable == 0:
        print("  ERROR: No usable audio files remain after integrity check. Aborting.")
        return False

    print(f"  {usable}/{len(audio_files)} file(s) passed integrity check.")
    return True


def run_fetch(config: dict, folder_name: str) -> bool:
    import fetch_episodes
    banner("STAGE 1 / 4 — Fetch & Download Episodes")
    t = time.time()
    try:
        fetch_episodes.fetch_and_download(config, folder_name=folder_name)
        print(f"\n[fetch] Done in {elapsed(t)}")
        return True
    except Exception:
        print("\n[fetch] FAILED:")
        traceback.print_exc()
        return False


def run_transcribe(config: dict, folder_name: str) -> bool | str:
    """Returns True (all OK), 'partial' (some failed verification), or False (crashed)."""
    import transcribe
    banner("STAGE 2 / 4 — Transcribe Audio")
    t = time.time()
    try:
        transcribe.transcribe_folder(config, folder_name)
        print(f"\n[transcribe] Done in {elapsed(t)}")
        return True
    except SystemExit as exc:
        print(f"\n[transcribe] Done in {elapsed(t)}")
        if exc.code == 2:
            # exit code 2 = verification found some failures, but not a crash
            return "partial"
        return False
    except Exception:
        print("\n[transcribe] FAILED:")
        traceback.print_exc()
        return False


def run_upload(config: dict, folder_name: str) -> "str | bool":
    """Returns notebook_id (str) on success, or False on failure."""
    import upload_to_notebooklm
    banner("STAGE 3 / 4 — Upload Transcripts to NotebookLM")
    t = time.time()
    try:
        notebook_id = upload_to_notebooklm.run(config, folder_name)
        print(f"\n[upload] Done in {elapsed(t)}")
        return notebook_id
    except SystemExit as exc:
        # upload_to_notebooklm calls sys.exit on auth failure — treat as error
        print(f"\n[upload] FAILED (exit {exc.code})")
        return False
    except Exception:
        print("\n[upload] FAILED:")
        traceback.print_exc()
        return False


def run_email(config: dict, folder_name: str, notebook_id: str,
              send_email_flag: bool = True) -> bool:
    import send_report
    banner("STAGE 4 / 4 — Generate Report & Send Email")
    t = time.time()
    try:
        send_report.run(config, folder_name, notebook_id, send_email_flag=send_email_flag)
        print(f"\n[email] Done in {elapsed(t)}")
        return True
    except Exception:
        print("\n[email] FAILED:")
        traceback.print_exc()
        return False


def _cutoff_date(months: int):
    """Return a date `months` calendar months before today."""
    today = datetime.now(timezone.utc).date()
    cutoff_month = today.month - months
    cutoff_year  = today.year
    while cutoff_month <= 0:
        cutoff_month += 12
        cutoff_year  -= 1
    return today.replace(year=cutoff_year, month=cutoff_month)


def _cleanup_data_dir(data_root: Path, label: str,
                      extensions: set[str], months: int) -> None:
    """Delete files matching `extensions` from week folders older than `months`."""
    if months <= 0:
        print(f"  {label}: retention = 0 (keep forever), skipping.")
        return

    banner(f"CLEANUP — {label} (keep {months} month{'s' if months != 1 else ''})")

    if not data_root.exists():
        print(f"  Directory not found: {data_root}")
        return

    cutoff = _cutoff_date(months)
    print(f"  Cutoff date : {cutoff}  (keeping folders on or after this date)")

    removed_folders = 0
    for week_dir in sorted(data_root.iterdir()):
        if not week_dir.is_dir():
            continue
        parts = week_dir.name.split("-")
        if len(parts) != 2 or len(parts[0]) != 8:
            print(f"  Skipping unrecognised folder: {week_dir.name}")
            continue
        try:
            folder_date = datetime.strptime(parts[0], "%Y%m%d").date()
        except ValueError:
            print(f"  Skipping unrecognised folder: {week_dir.name}")
            continue

        if folder_date < cutoff:
            files = []
            for ext in extensions:
                files.extend(week_dir.glob(f"*{ext}"))
            if files:
                total_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)
                print(f"  Deleting {len(files)} file(s) ({total_mb:.1f} MB) from {week_dir.name}")
                for f in files:
                    f.unlink()
                removed_folders += 1
            else:
                print(f"  Already clean: {week_dir.name}")
        else:
            print(f"  Keeping : {week_dir.name}")

    if removed_folders == 0:
        print("\n  No old files to remove.")
    else:
        print(f"\n  Cleaned up {removed_folders} folder(s).")


def _cleanup_by_speaker(data_root: Path, label: str,
                        extensions: set[str], months: int) -> None:
    """Delete files matching `extensions` from per-speaker subdirs older than `months`."""
    if months <= 0:
        print(f"  {label}: retention = 0 (keep forever), skipping.")
        return

    banner(f"CLEANUP — {label} (keep {months} month{'s' if months != 1 else ''})")

    if not data_root.exists():
        print(f"  Directory not found: {data_root}")
        return

    cutoff = _cutoff_date(months)
    print(f"  Cutoff date : {cutoff}  (deleting files published before this date)")

    removed = 0
    for speaker_dir in sorted(data_root.iterdir()):
        if not speaker_dir.is_dir():
            continue
        for ext in extensions:
            for f in speaker_dir.glob(f"*{ext}"):
                date_str = f.stem.split("_")[-1]
                try:
                    file_date = datetime.strptime(date_str, "%Y%m%d").date()
                except ValueError:
                    continue
                if file_date < cutoff:
                    size_mb = f.stat().st_size / (1024 * 1024)
                    print(f"  Deleting ({size_mb:.2f} MB): {speaker_dir.name}/{f.name}")
                    f.unlink()
                    removed += 1

    if removed == 0:
        print("  No old files to remove.")
    else:
        print(f"  Removed {removed} file(s).")


def _cleanup_audio_by_speaker(audio_root: Path, months: int) -> None:
    _cleanup_by_speaker(audio_root, "Audio", SUPPORTED_AUDIO_EXTS, months)


def cleanup_old_data(config: dict) -> bool:
    """Clean up old audio, transcript, and report files per retention config."""
    retention = config.get("retention", {})
    parent    = Path(config["parent_folder"])

    _cleanup_audio_by_speaker(
        parent / "audio",
        int(retention.get("audio_months", 3)),
    )
    _cleanup_by_speaker(
        parent / "transcripts",
        "Transcripts",
        {".txt"},
        int(retention.get("transcripts_months", 0)),
    )
    _cleanup_data_dir(
        parent / "reports",
        "Reports",
        {".txt", ".html", ".md"},
        int(retention.get("reports_months", 0)),
    )
    return True


# Keep the old name as an alias so existing callers aren't broken
def cleanup_old_audio(config: dict) -> bool:
    return cleanup_old_data(config)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="End-to-end podcast pipeline: fetch → transcribe → NotebookLM upload."
    )
    parser.add_argument("--config", default="config.yaml",
                        help="Path to config YAML (default: config.yaml)")
    parser.add_argument("--folder", default=None,
                        help="Run folder, e.g. 20260218-20260225. Defaults to the current lookback window.")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Skip the fetch/download stage.")
    parser.add_argument("--skip-transcribe", action="store_true",
                        help="Skip the transcription stage.")
    parser.add_argument("--skip-upload", action="store_true",
                        help="Skip the NotebookLM upload stage.")
    parser.add_argument("--skip-email", action="store_true",
                        help="Skip the report generation and email stage.")
    parser.add_argument("--notebook-id", default=None,
                        help="Reuse an existing notebook ID (skips upload, implies --skip-upload).")
    parser.add_argument("--skip-cleanup", action="store_true",
                        help="Skip the data cleanup stage.")
    parser.add_argument("--save-report-only", action="store_true",
                        help="Generate and save the report to disk without sending email.")
    args = parser.parse_args()

    config = load_config(args.config)
    folder_name = args.folder or default_folder_name(int(config.get("lookback_days", 7)))

    # If a notebook ID is provided directly, skip the upload stage
    if args.notebook_id:
        args.skip_upload = True

    pipeline_start = time.time()

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║           Stock Weekly Report — Pipeline                 ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"  Config     : {args.config}")
    print(f"  Run folder : {folder_name}")
    print(f"  Stages     : "
          + ("fetch " if not args.skip_fetch else "")
          + ("transcribe " if not args.skip_transcribe else "")
          + ("upload " if not args.skip_upload else "")
          + ("email " if not args.skip_email else "")
          + ("cleanup" if not args.skip_cleanup else ""))

    results: dict[str, bool | str] = {}
    notebook_id: str | None = args.notebook_id

    # ── Stage 1: Fetch ──────────────────────────────────────────────
    if not args.skip_fetch:
        ok = run_fetch(config, folder_name)
        results["fetch"] = ok
        if not ok:
            print("\nPipeline aborted after fetch failure.")
            _print_summary(results, pipeline_start)
            sys.exit(1)
    else:
        results["fetch"] = "skipped"

    # ── Guard: Audio integrity ───────────────────────────────────────
    if not args.skip_transcribe:
        ok = validate_audio_files(config, folder_name)
        results["audio_check"] = ok
        if not ok:
            print("\nPipeline aborted: audio integrity check failed.")
            _print_summary(results, pipeline_start)
            sys.exit(1)

    # ── Stage 2: Transcribe ─────────────────────────────────────────
    if not args.skip_transcribe:
        ok = run_transcribe(config, folder_name)
        results["transcribe"] = ok
        if not ok:
            print("\nPipeline aborted after transcribe failure.")
            _print_summary(results, pipeline_start)
            sys.exit(1)
    else:
        results["transcribe"] = "skipped"

    # ── Stage 3: Upload ─────────────────────────────────────────────
    if not args.skip_upload:
        result = run_upload(config, folder_name)
        if result is False:
            results["upload"] = False
            _print_summary(results, pipeline_start)
            sys.exit(1)
        notebook_id = result
        results["upload"] = True
    else:
        results["upload"] = "skipped"

    # ── Stage 4: Report & Email ─────────────────────────────────────
    if not args.skip_email:
        if not notebook_id:
            print("\n[email] Skipped: no notebook ID available (upload was skipped).")
            print("  Re-run with --notebook-id <id> to send the email separately.")
            results["email"] = "skipped"
        else:
            ok = run_email(config, folder_name, notebook_id,
                           send_email_flag=not args.save_report_only)
            results["email"] = ok
    else:
        results["email"] = "skipped"

    # ── Cleanup: Remove old audio / transcripts / reports ───────────
    if not args.skip_cleanup:
        cleanup_old_data(config)
        results["cleanup"] = True
    else:
        results["cleanup"] = "skipped"

    _print_summary(results, pipeline_start)
    if results.get("upload") is False or results.get("email") is False:
        sys.exit(1)


def _print_summary(results: dict, start: float) -> None:
    banner("Pipeline Summary")
    icons = {True: "✓", False: "✗", "skipped": "–", "partial": "~"}
    for stage, status in results.items():
        icon = icons.get(status, "?")
        if status is True:
            label = "OK"
        elif status is False:
            label = "FAILED"
        elif status == "partial":
            label = "partial (some files failed)"
        else:
            label = str(status)
        print(f"  {icon}  {stage:<12} {label}")
    print(f"\n  Total time: {elapsed(start)}")
    print()


if __name__ == "__main__":
    main()
