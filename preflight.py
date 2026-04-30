from __future__ import annotations
"""
preflight.py

Read-only sanity check for a psum config. Verifies that every external
dependency the next pipeline run will hit is reachable and authenticated,
without burning a full pipeline run.

Used by `psum preflight [CONFIG]`.

The script is invoked by the *pipeline venv* (same venv cron will use), so
the imports at the top of this file are themselves a check — if `requests`,
`feedparser`, `yt_dlp`, `openai`, `faster_whisper`, or `imageio_ffmpeg`
can't be loaded, this script won't even start, which is exactly the
signal we want.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

import feedparser
import requests
import yaml

# These are imported as a module-load smoke-test for the pipeline venv;
# `pipeline.py` and friends will need them at runtime.
import yt_dlp                 # noqa: F401
import openai                 # noqa: F401  (we don't make a client here, just verify import)
import faster_whisper         # noqa: F401
import imageio_ffmpeg         # noqa: F401
import markdown               # noqa: F401


# ---------------------------------------------------------------------------
# Result helpers — every check returns a CheckResult.
# ---------------------------------------------------------------------------

class CheckResult:
    __slots__ = ("name", "ok", "detail", "skipped", "warning")

    def __init__(
        self,
        name: str,
        ok: bool,
        detail: str = "",
        *,
        skipped: bool = False,
        warning: bool = False,
    ) -> None:
        self.name = name
        self.ok = ok
        self.detail = detail
        self.skipped = skipped
        self.warning = warning

    @property
    def icon(self) -> str:
        if self.skipped:
            return "–"
        if self.warning:
            return "!"
        return "✓" if self.ok else "✗"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_env_vars(cfg: dict) -> CheckResult:
    """Source ~/.zshenv (and ~/.zprofile fallback) in a clean shell, like run.sh
    does, and confirm every env var the config relies on is present."""
    needed: list[tuple[str, bool]] = []
    if cfg.get("image_generation", {}).get("enabled"):
        needed.append(("OPENAI_API_KEY", True))
    if cfg.get("instagram", {}).get("enabled"):
        needed.append(("INSTAGRAM_ACCESS_TOKEN", True))
    if cfg.get("email", {}).get("enabled"):
        # Only required if smtp_password is empty in the config.
        if not cfg.get("email", {}).get("smtp_password"):
            needed.append(("EMAIL_SMTP_PASSWORD", True))

    if not needed:
        return CheckResult(
            "Env vars",
            True,
            "no env vars required by this config (image / instagram / email all "
            "either disabled or have inline credentials).",
            skipped=True,
        )

    # Run a clean bash that mirrors run.sh's lines.
    cmd = (
        "[ -f ~/.zshenv ] && . ~/.zshenv; "
        "[ -f ~/.zprofile ] && . ~/.zprofile; "
        "env"
    )
    try:
        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=10,
            env={"HOME": os.environ.get("HOME", ""), "PATH": "/usr/bin:/bin"},
        )
    except Exception as exc:
        return CheckResult("Env vars", False, f"shell probe failed: {exc}")

    sourced_env = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in result.stdout.splitlines()
        if "=" in line
    }
    missing = [name for name, _ in needed if not sourced_env.get(name)]
    if missing:
        return CheckResult(
            "Env vars",
            False,
            f"sourcing ~/.zshenv (+ ~/.zprofile) does not provide: {', '.join(missing)}. "
            f"Either re-run `psum init` for that section or add `export {missing[0]}=...` to ~/.zshenv.",
        )
    present = ", ".join(name for name, _ in needed)
    return CheckResult("Env vars", True, f"{present} (sourced from ~/.zshenv).")


def check_pipeline_imports(cfg: dict) -> CheckResult:
    """If we got this far, every required pipeline import already succeeded
    (the imports are at the top of this file). Just report what's loaded."""
    return CheckResult(
        "Pipeline venv imports",
        True,
        "feedparser, requests, yt_dlp, openai, faster_whisper, imageio_ffmpeg, markdown OK.",
    )


def check_nlm_auth(cfg: dict) -> CheckResult:
    nlm_path = cfg.get("nlm_path") or "nlm"
    try:
        result = subprocess.run(
            [nlm_path, "login", "--check"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return CheckResult(
            "NotebookLM auth",
            False,
            f"`{nlm_path}` binary not found. Reinstall: pip install -e . in the CLI venv.",
        )
    except Exception as exc:
        return CheckResult("NotebookLM auth", False, f"could not run nlm: {exc}")

    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip().splitlines()
        tail = msg[-1] if msg else "(no output)"
        return CheckResult(
            "NotebookLM auth",
            False,
            f"`nlm login --check` exited {result.returncode} — {tail}. "
            "Re-authenticate with: psum nlm-login",
        )

    summary_line = ""
    for line in (result.stdout or "").splitlines():
        if "Account:" in line or "Notebooks found:" in line:
            summary_line += line.strip() + "; "
    summary_line = summary_line.rstrip("; ") or "valid."
    return CheckResult("NotebookLM auth", True, summary_line)


def check_openai(cfg: dict) -> CheckResult:
    if not cfg.get("image_generation", {}).get("enabled"):
        return CheckResult("OpenAI API", True, "image_generation disabled.", skipped=True)
    key = os.environ.get("OPENAI_API_KEY") or cfg.get("image_generation", {}).get("api_key")
    if not key:
        return CheckResult(
            "OpenAI API",
            False,
            "OPENAI_API_KEY not in environment and not in image_generation.api_key.",
        )
    try:
        r = requests.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
        )
    except Exception as exc:
        return CheckResult("OpenAI API", False, f"network error: {exc}")
    if r.status_code == 200:
        n = len(r.json().get("data", []))
        return CheckResult("OpenAI API", True, f"HTTP 200 from /v1/models ({n} models visible).")
    if r.status_code == 401:
        return CheckResult(
            "OpenAI API",
            False,
            "HTTP 401 — the OPENAI_API_KEY is invalid or revoked. "
            "Rotate it and re-run `psum init` (image section).",
        )
    if r.status_code == 429:
        return CheckResult(
            "OpenAI API",
            True,
            "HTTP 429 — rate-limited but the key is recognised. Tomorrow's run "
            "may still succeed; consider quota.",
            warning=True,
        )
    return CheckResult("OpenAI API", False, f"unexpected HTTP {r.status_code}: {r.text[:160]}")


def check_instagram(cfg: dict) -> CheckResult:
    ig_cfg = cfg.get("instagram", {})
    if not ig_cfg.get("enabled"):
        return CheckResult("Instagram token", True, "instagram disabled.", skipped=True)
    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN") or ig_cfg.get("access_token")
    if not token:
        return CheckResult(
            "Instagram token",
            False,
            "INSTAGRAM_ACCESS_TOKEN not in environment and not in instagram.access_token.",
        )
    api_version = ig_cfg.get("api_version", "v21.0")
    try:
        r = requests.get(
            f"https://graph.instagram.com/{api_version}/me",
            params={"access_token": token, "fields": "id,username"},
            timeout=15,
        )
    except Exception as exc:
        return CheckResult("Instagram token", False, f"network error: {exc}")
    if r.status_code == 200:
        data = r.json()
        return CheckResult(
            "Instagram token",
            True,
            f"@{data.get('username', '?')} (id={data.get('id', '?')}).",
        )
    return CheckResult(
        "Instagram token",
        False,
        f"HTTP {r.status_code}: {r.text[:200]} — token likely expired (60-day TTL). "
        "Generate a new long-lived token and re-run `psum init` (instagram section).",
    )


def check_feeds(cfg: dict) -> CheckResult:
    feeds = cfg.get("feeds") or []
    if not feeds:
        return CheckResult("Feed reachability", True, "no feeds configured.", skipped=True)

    failures: list[str] = []
    youtube_failures: list[str] = []
    for feed in feeds:
        name = feed.get("name", "?")
        url = feed.get("url", "")
        ftype = feed.get("type", "").lower()
        if not url:
            failures.append(f"{name} (no url)")
            continue
        try:
            head = requests.head(url, allow_redirects=True, timeout=10,
                                 headers={"User-Agent": "Mozilla/5.0"})
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}")
            continue
        if not head.ok and head.status_code not in (405,):  # some hosts disallow HEAD
            failures.append(f"{name}: HTTP {head.status_code}")
            continue
        # YouTube: also probe the per-channel Atom feed. If the channel page
        # is reachable but the feed isn't, fetch will fall back to yt-dlp,
        # which works but is much slower and rate-limit-prone.
        if ftype == "youtube":
            try:
                ydl_opts = {
                    "extract_flat": True, "quiet": True, "no_warnings": True,
                    "playlist_items": "0",
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False) or {}
                channel_id = info.get("channel_id")
                if channel_id:
                    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
                    rss = requests.get(feed_url, timeout=15)
                    if rss.status_code != 200:
                        youtube_failures.append(f"{name}: Atom feed HTTP {rss.status_code}")
            except Exception:
                pass  # not fatal — fetch_youtube has yt-dlp fallback

    detail_parts: list[str] = [f"{len(feeds) - len(failures)}/{len(feeds)} reachable"]
    if youtube_failures:
        detail_parts.append(f"YouTube Atom feed degraded: {len(youtube_failures)} (yt-dlp fallback will be used)")
    if failures:
        return CheckResult(
            "Feed reachability",
            False,
            f"{'; '.join(failures)}. {' / '.join(detail_parts)}.",
        )
    return CheckResult(
        "Feed reachability",
        True,
        " / ".join(detail_parts) + ".",
        warning=bool(youtube_failures),
    )


def check_image_hosts(cfg: dict) -> CheckResult:
    if not cfg.get("image_generation", {}).get("enabled"):
        return CheckResult("Image hosts", True, "image generation disabled.", skipped=True)
    if not cfg.get("instagram", {}).get("enabled"):
        return CheckResult("Image hosts", True, "instagram disabled — no upload step.", skipped=True)

    hosts = [
        ("tmpfiles.org", "https://tmpfiles.org/"),
        ("catbox.moe",   "https://catbox.moe/"),
    ]
    statuses = []
    up = 0
    for name, url in hosts:
        try:
            r = requests.head(url, allow_redirects=True, timeout=10)
            if r.ok or r.status_code in (403, 405):  # some host roots reject HEAD
                statuses.append(f"{name}: up")
                up += 1
            else:
                statuses.append(f"{name}: HTTP {r.status_code}")
        except Exception as exc:
            statuses.append(f"{name}: {type(exc).__name__}")
    detail = "; ".join(statuses)
    if up == 0:
        return CheckResult(
            "Image hosts",
            False,
            detail + ". Both image hosts are unreachable — Instagram posting will fail.",
        )
    if up < len(hosts):
        return CheckResult(
            "Image hosts",
            True,
            detail + ". Fallback chain still has a working host.",
            warning=True,
        )
    return CheckResult("Image hosts", True, detail + ".")


def check_logs_dir(cfg: dict) -> CheckResult:
    project_root = Path(cfg.get("project_root") or Path(__file__).resolve().parent)
    logs_dir = project_root / "logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return CheckResult("Logs dir writable", False, f"{logs_dir}: {exc}")
    if not os.access(logs_dir, os.W_OK):
        return CheckResult("Logs dir writable", False, f"{logs_dir} exists but is not writable.")
    return CheckResult("Logs dir writable", True, str(logs_dir))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

CHECKS: list[Callable[[dict], CheckResult]] = [
    check_env_vars,
    check_pipeline_imports,
    check_nlm_auth,
    check_openai,
    check_instagram,
    check_feeds,
    check_image_hosts,
    check_logs_dir,
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-flight check for a psum config — verifies the next pipeline run can succeed."
    )
    parser.add_argument("--config", required=True, help="Path to the config YAML.")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    config_label = Path(args.config).stem
    print(f"\n=== psum preflight ({config_label}) ===\n")

    results: list[CheckResult] = []
    for fn in CHECKS:
        try:
            r = fn(cfg)
        except Exception as exc:
            r = CheckResult(fn.__name__, False, f"unexpected error: {type(exc).__name__}: {exc}")
        results.append(r)
        # Pretty-print as we go so the user sees progress.
        line = f"  {r.icon}  {r.name}"
        if r.detail:
            line += f"  — {r.detail}"
        print(line)

    print()
    print("  ℹ  Note: cron only fires when the machine is awake. macOS sleep can skip runs;")
    print("     plug in / disable App Nap on Terminal if you want guaranteed 08:00 firing.\n")

    failed = [r for r in results if not r.ok and not r.skipped]
    warnings = [r for r in results if r.warning]
    if failed:
        print(f"→ {len(failed)} check(s) failed. Fix the items above before tomorrow's run.\n")
        return 1
    if warnings:
        print(f"→ {len(warnings)} warning(s). The run will likely succeed but you may see degraded behaviour.\n")
        return 0
    print("→ All checks passed. Tomorrow's scheduled run should proceed.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
