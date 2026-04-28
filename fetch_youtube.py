from __future__ import annotations
"""
fetch_youtube.py

Fetches recent videos from a YouTube channel within the pipeline date window
and produces transcripts in the same structure as the whisper stage, so the
rest of the pipeline (upload, report, email) is unchanged.

Strategy per video:
  1. Try youtube-transcript-api (fast, no download) → save directly to transcripts/
  2. Fall back to yt-dlp audio download → saved to audio/ for the whisper stage

Transcript files land at:
  {source_folder}/transcripts/{channel_name}/{channel_name}_{videoId}_{YYYYMMDD}.txt

Audio fallback files land at:
  {source_folder}/audio/{channel_name}/{channel_name}_{videoId}_{YYYYMMDD}.mp3

Both paths are compatible with upload_to_notebooklm.py's date-based discovery
(stem.split("_")[-1] == YYYYMMDD) and transcribe.py's parent-directory speaker lookup.
"""

import re
from datetime import date
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def _resolve_channel_id(channel_url: str) -> str | None:
    """Resolve a @handle / /c/name / /channel/ID URL to a YouTube channel ID."""
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError(
            "yt-dlp is required for YouTube channel support.\n"
            "  Install: pip install yt-dlp  (or add to requirements.txt)"
        )

    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "playlist_items": "0",  # don't list any videos, just the channel metadata
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(channel_url, download=False) or {}
    return info.get("channel_id") or info.get("uploader_id")


def _list_channel_videos(
    channel_url: str,
    start_date: date,
    end_date: date,
) -> list[dict]:
    """Return [{id, title, upload_date}] for videos published in [start_date, end_date].

    Uses YouTube's per-channel RSS feed (https://www.youtube.com/feeds/videos.xml)
    which is much faster than yt-dlp full-extraction. The feed surfaces the latest
    ~15 videos with proper publication dates and video IDs.
    """
    import requests
    import feedparser
    from datetime import datetime, timezone

    channel_id = _resolve_channel_id(channel_url)
    if not channel_id:
        raise RuntimeError(f"Could not resolve channel ID for {channel_url}")

    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    resp = requests.get(rss_url, timeout=30)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)

    videos = []
    for entry in parsed.entries:
        # feedparser exposes the YouTube namespace as yt_videoid
        video_id = getattr(entry, "yt_videoid", None) or entry.get("id", "").split(":")[-1]
        if not video_id:
            continue

        # YouTube's Atom feed uses ISO 8601 dates (not RFC 2822 like most RSS feeds).
        published_raw = entry.get("published") or entry.get("updated")
        if not published_raw:
            continue
        try:
            dt = datetime.fromisoformat(published_raw)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc)
            pub_date = dt.date()
        except Exception:
            continue

        if not (start_date <= pub_date <= end_date):
            continue

        videos.append({
            "id": video_id,
            "title": entry.get("title", video_id),
            "upload_date": pub_date.strftime("%Y%m%d"),
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })
    return videos


def _fetch_via_transcript_api(video_id: str, language: str) -> str | None:
    """Return caption text, or None if captions are unavailable."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        parts = YouTubeTranscriptApi.get_transcript(
            video_id, languages=[language, "en"]
        )
        return " ".join(p["text"] for p in parts).strip() or None
    except Exception:
        return None


def _resolve_ffmpeg_location() -> str | None:
    """Return a path to ffmpeg yt-dlp can use, or None if it must rely on $PATH.

    Prefer system ffmpeg, then fall back to the imageio-ffmpeg bundled binary.
    """
    import shutil
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return None  # let yt-dlp find it on PATH
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _download_audio(video_url: str, dest: Path) -> bool:
    """Download best-quality audio to dest (.mp3). Returns True on success."""
    try:
        import yt_dlp
    except ImportError:
        return False

    # yt-dlp appends the extension itself, so strip it from the template
    template = str(dest.with_suffix(""))
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": template,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }],
        "quiet": True,
        "no_warnings": True,
    }
    ffmpeg_loc = _resolve_ffmpeg_location()
    if ffmpeg_loc:
        ydl_opts["ffmpeg_location"] = ffmpeg_loc

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
        return True
    except Exception as exc:
        print(f"    yt-dlp error: {exc}")
        return False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fetch_channel(config: dict, feed: dict, folder_name: str) -> None:
    """Fetch videos from a YouTube channel for the pipeline date window.

    Transcripts fetched via the API are written directly to transcripts/.
    Videos without captions have their audio downloaded to audio/ for the
    existing whisper transcription stage.
    """
    channel_url = feed["url"]
    channel_name = feed["name"]
    language = feed.get("language") or config.get("whisper_language", "en")

    source_folder = Path(config["source_folder"])
    transcript_dir = source_folder / "transcripts" / channel_name
    audio_dir = source_folder / "audio" / channel_name

    parts = folder_name.split("-")
    start_date = date(int(parts[0][:4]), int(parts[0][4:6]), int(parts[0][6:]))
    end_date = date(int(parts[1][:4]), int(parts[1][4:6]), int(parts[1][6:]))

    print(f"[{channel_name}] Fetching YouTube channel …")
    print(f"  URL        : {channel_url}")
    print(f"  Date range : {parts[0]} → {parts[1]}")

    try:
        videos = _list_channel_videos(channel_url, start_date, end_date)
    except RuntimeError as exc:
        print(f"  ERROR: {exc}")
        print()
        return

    if not videos:
        print(f"  No videos published in this date range.")
        print()
        return

    print(f"  Found {len(videos)} video(s)")

    for video in videos:
        vid_id = video["id"]
        upload_date = video["upload_date"]
        # Filename: {channel_name}_{videoId}_{YYYYMMDD}
        # stem.split("_")[-1] == upload_date — compatible with all pipeline stages
        stem = f"{channel_name}_{vid_id}_{upload_date}"

        transcript_path = transcript_dir / f"{stem}.txt"
        audio_path = audio_dir / f"{stem}.mp3"

        if transcript_path.exists():
            print(f"  [skip] Transcript exists : {transcript_path.name}")
            continue
        if audio_path.exists():
            print(f"  [skip] Audio exists      : {audio_path.name}")
            continue

        print(f"  [{upload_date}] {video['title'][:72]}")

        # Try captions first (fast, no download)
        text = _fetch_via_transcript_api(vid_id, language)
        if text:
            transcript_dir.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text(text, encoding="utf-8")
            print(f"    ✓ Transcript via API  : {transcript_path.name}  ({len(text):,} chars)")
        else:
            # No captions — download audio for the whisper stage
            print(f"    No captions — downloading audio for Whisper …")
            audio_dir.mkdir(parents=True, exist_ok=True)
            ok = _download_audio(video["url"], audio_path)
            if ok:
                print(f"    ✓ Audio saved         : {audio_path.name}")
            else:
                print(f"    WARNING: Could not get transcript or audio for: {video['title']}")

    print()
