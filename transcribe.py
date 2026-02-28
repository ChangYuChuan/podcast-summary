from __future__ import annotations
"""
transcribe.py

Transcribes audio files produced by fetch_episodes.py using faster-whisper
(CTranslate2 backend — faster and more memory-efficient than openai-whisper).

Features:
  - Up to MAX_RETRIES attempts per file on failure
  - Post-run verification: checks every expected transcript is non-empty
  - Partial/corrupt transcripts are removed before a retry

Usage:
  python transcribe.py                        # auto-detect latest run folder
  python transcribe.py --folder 20260218-20260225
  python transcribe.py --config my_config.yaml --folder 20260218-20260225

Input structure:
  {parent_folder}/audio/{program_name}/{program_name}_{YYYYMMDD}.ext

Output structure:
  {parent_folder}/transcripts/{YYYYMMDD}-{YYYYMMDD}/{stem}.txt
"""

import argparse
import sys
import time
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path

SUPPORTED_AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".aac", ".wav", ".flac", ".opus"}

# Minimum character count for a transcript to be considered valid.
# A podcast episode that produces fewer chars is almost certainly a failed run.
MIN_TRANSCRIPT_CHARS = 50

MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds between retries


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


def find_audio_files_for_run(audio_root: Path, folder_name: str) -> list[Path]:
    """Collect audio files across per-speaker subdirs whose date falls in the run window.

    Expects filenames of the form {speaker}_{YYYYMMDD}.ext so the date can be
    extracted from the stem suffix.
    """
    parts = folder_name.split("-")
    start_str, end_str = parts[0], parts[1]

    files = []
    if not audio_root.exists():
        return files
    for speaker_dir in sorted(audio_root.iterdir()):
        if not speaker_dir.is_dir():
            continue
        for ext in SUPPORTED_AUDIO_EXTS:
            for f in speaker_dir.glob(f"*{ext}"):
                date_str = f.stem.split("_")[-1]
                if len(date_str) == 8 and start_str <= date_str <= end_str:
                    files.append(f)
    return sorted(files)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_transcript(transcript_path: Path) -> tuple[bool, str]:
    """Check that a transcript file exists and has meaningful content.

    Returns (ok, reason) where reason is an empty string on success.
    """
    if not transcript_path.exists():
        return False, "file missing"

    size = transcript_path.stat().st_size
    if size == 0:
        return False, "file is empty (0 bytes)"

    text = transcript_path.read_text(encoding="utf-8").strip()
    if len(text) < MIN_TRANSCRIPT_CHARS:
        return False, f"suspiciously short ({len(text)} chars < {MIN_TRANSCRIPT_CHARS})"

    return True, ""


def verify_all(audio_files: list[Path], transcript_root: Path) -> dict[str, tuple[bool, str]]:
    """Verify every expected transcript and return a {filename: (ok, reason)} map."""
    results = {}
    for audio_file in audio_files:
        stem = audio_file.stem
        speaker = stem.rsplit("_", 1)[0]
        transcript_path = transcript_root / speaker / f"{stem}.txt"
        results[audio_file.name] = verify_transcript(transcript_path)
    return results


# ---------------------------------------------------------------------------
# Single-file transcription with retry
# ---------------------------------------------------------------------------

def _do_transcribe(model, audio_file: Path, language: str) -> str:
    """Run transcription and return the full text. Raises on error."""
    segments, info = model.transcribe(
        str(audio_file),
        language=language,
        beam_size=5,
    )
    print(f"  Detected language: {info.language} (prob {info.language_probability:.2f})")
    return "".join(seg.text for seg in segments)


def transcribe_with_retry(
    model,
    audio_file: Path,
    transcript_path: Path,
    language: str,
    max_retries: int = MAX_RETRIES,
) -> bool:
    """Transcribe one file, retrying on failure or empty output.

    Returns True on success, False if all attempts failed.
    """
    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            print(f"  Retry {attempt - 1}/{max_retries - 1} — waiting {RETRY_DELAY}s …")
            time.sleep(RETRY_DELAY)

        # Remove any partial/corrupt file from a previous attempt
        if transcript_path.exists():
            transcript_path.unlink()

        try:
            text = _do_transcribe(model, audio_file, language)
        except Exception as exc:
            print(f"  Attempt {attempt} ERROR: {exc}")
            continue

        if len(text.strip()) < MIN_TRANSCRIPT_CHARS:
            print(f"  Attempt {attempt} produced too little text ({len(text.strip())} chars) — retrying")
            continue

        transcript_path.write_text(text, encoding="utf-8")
        return True

    return False


# ---------------------------------------------------------------------------
# Main transcription routine
# ---------------------------------------------------------------------------

def transcribe_folder(config: dict, folder_name: str) -> None:
    # Import here so the script is importable without faster-whisper installed
    from faster_whisper import WhisperModel

    parent_folder = Path(config["parent_folder"])
    audio_root = parent_folder / "audio"
    transcript_root = parent_folder / "transcripts"

    audio_files = find_audio_files_for_run(audio_root, folder_name)
    if not audio_files:
        print(f"No audio files found in {audio_root} for run window {folder_name}")
        return

    model_name = config.get("whisper_model", "medium")
    language = config.get("whisper_language", "zh")
    compute_type = config.get("whisper_compute_type", "int8")

    print(f"Whisper model   : {model_name}  (faster-whisper / CTranslate2)")
    print(f"Compute type    : {compute_type}")
    print(f"Language hint   : {language}")
    print(f"Audio root      : {audio_root}")
    print(f"Transcript root : {transcript_root}")
    print(f"Files to process: {len(audio_files)}")
    print(f"Max retries     : {MAX_RETRIES}")
    print()

    print(f"Loading model '{model_name}' …")
    model = WhisperModel(model_name, device="cpu", compute_type=compute_type)
    print("Model loaded.\n")

    succeeded, skipped, failed = [], [], []

    for idx, audio_file in enumerate(audio_files, start=1):
        stem = audio_file.stem                          # e.g. "股癌_20260225"
        speaker = stem.rsplit("_", 1)[0]               # e.g. "股癌"
        speaker_transcript_dir = transcript_root / speaker
        speaker_transcript_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = speaker_transcript_dir / f"{stem}.txt"
        label = f"[{idx}/{len(audio_files)}]"

        # Check if an existing transcript already passes verification
        if transcript_path.exists():
            ok, reason = verify_transcript(transcript_path)
            if ok:
                print(f"{label} SKIP (valid transcript exists): {audio_file.name}")
                skipped.append(audio_file.name)
                continue
            else:
                print(f"{label} Re-transcribing (existing transcript invalid — {reason}): {audio_file.name}")

        print(f"{label} Transcribing: {audio_file.name} …")
        ok = transcribe_with_retry(model, audio_file, transcript_path, language)

        if ok:
            print(f"  Saved: {transcript_path}")
            succeeded.append(audio_file.name)
        else:
            print(f"  FAILED after {MAX_RETRIES} attempt(s): {audio_file.name}")
            failed.append(audio_file.name)
        print()

    # ── Post-run verification ─────────────────────────────────────────────
    print("─" * 60)
    print("Verification pass …")
    verification = verify_all(audio_files, transcript_root)

    all_ok = True
    for audio_name, (ok, reason) in verification.items():
        status = "✓" if ok else "✗"
        detail = f"  — {reason}" if not ok else ""
        print(f"  {status}  {audio_name}{detail}")
        if not ok:
            all_ok = False

    print()
    print(f"Transcription complete — "
          f"{len(succeeded)} new, {len(skipped)} skipped, {len(failed)} failed.")

    if not all_ok:
        print("\nWARNING: One or more transcripts failed verification.")
        print("Re-run this script to retry only the failed files.")
        sys.exit(2)  # exit code 2 = partial failure (distinguishable from crash)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transcribe podcast audio files using Whisper."
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

    folder_name = args.folder or default_folder_name(
        int(config.get("lookback_days", 7))
    )

    print(f"Run folder: {folder_name}\n")
    transcribe_folder(config, folder_name)


if __name__ == "__main__":
    main()
