from __future__ import annotations
"""
generate_image.py

Stage 5 (optional): Generate one image per report section using OpenAI's
image generation API, producing a cohesive set suitable for an Instagram carousel.

The report is split into its sections (Episode Summaries, Key Themes, Takeaways, …).
Each section gets its own image built from that section's content, but all images
share the same visual-style description so the set looks consistent.

Requires:
  - openai Python package  (pip install openai)
  - OPENAI_API_KEY environment variable

Config keys (all optional except enabled):
  image_generation:
    enabled: true
    model: gpt-image-2           # or dall-e-3, gpt-image-1, etc.
    size: 1024x1024
    quality: standard            # standard | hd  (model-dependent)
    style: infographic           # infographic (readable summary card) | illustration (abstract art)
    max_images: 10               # cap on images; one per section up to this limit
    style_base: "..."            # optional: override the shared visual-style description
    prompt_template: "..."       # optional: per-section prompt override
                                 # placeholders: {report_title} {date_range} {section_title}
                                 #               {card_label} {highlights} {themes}
"""

import base64
import os
import re
import urllib.request
from pathlib import Path

import requests


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _format_date_range(folder_name: str) -> str:
    try:
        start, end = folder_name.split("-")
        return f"{start[:4]}/{start[4:6]}/{start[6:]} – {end[:4]}/{end[4:6]}/{end[6:]}"
    except Exception:
        return folder_name


def _parse_sections(summary: str) -> list[tuple[str, str]]:
    """Split the report into (section_title, section_body) pairs.

    Splits on top-level `## ` markdown headers instead of `---` separators
    because NotebookLM responses often contain horizontal-rule separators
    inside a single section's body (e.g. between bullish/bearish/watch
    subsections of the stocks list). Splitting on `---` would chop one
    section into many.
    """
    pattern = re.compile(r"^## +(.+?)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(summary))
    sections: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(summary)
        body = summary[body_start:body_end]
        # Trim trailing query_all_sections separators (`\n\n---\n\n`) so
        # they don't end up rendered as part of the body.
        body = re.sub(r"(\s*\n\s*-{3,}\s*)+\s*$", "", body).strip()
        if title and body:
            sections.append((title, body))
    return sections


def _clean_body(body: str) -> str:
    """Remove sub-headers and bold markers from a section body."""
    body = re.sub(r"^#{1,4}\s+.+$", "", body, flags=re.MULTILINE)
    body = re.sub(r"\*\*(.+?)\*\*", r"\1", body)
    return body


# ---------------------------------------------------------------------------
# Content extractors (operate on a single section body)
# ---------------------------------------------------------------------------

def _highlights_from_body(body: str, max_items: int = 6) -> str:
    """Extract up to max_items bullet points from a section body."""
    body = _clean_body(body)
    bullets: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        m = re.match(r"^[-*•]\s+(.+)$", line) or re.match(r"^\d+\.\s+(.+)$", line)
        if m:
            text = m.group(1).strip()
            if len(text) > 90:
                text = text[:87] + "…"
            if text:
                bullets.append(text)
        if len(bullets) >= max_items:
            break

    if bullets:
        return "\n".join(f"• {b}" for b in bullets)

    # Fallback: clean prose excerpt
    cleaned = re.sub(r"^\s*[-*•]\s+", "", body, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", cleaned).strip()[:400]


def _themes_from_body(body: str) -> str:
    """Return clean prose from a section body for illustration-style prompts."""
    body = _clean_body(body)
    cleaned = re.sub(r"^\s*[-*•]\s+", "", body, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", cleaned).strip()[:500]


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _is_chinese(config: dict) -> bool:
    """Return True when the report's working language is Chinese."""
    lang = config.get("image_generation", {}).get("language") or config.get(
        "whisper_language", "en"
    )
    return lang.lower().startswith("zh")


def _style_prefix(config: dict) -> str:
    """Shared visual-style description applied to every image in the set.

    Being identical across all prompts is what makes the generated cards look
    like a cohesive series rather than unrelated images.
    """
    image_cfg = config.get("image_generation", {})
    if image_cfg.get("style_base"):
        return image_cfg["style_base"]
    chinese = _is_chinese(config)
    if image_cfg.get("style", "infographic") == "illustration":
        if chinese:
            return (
                "可愛、柔和的編輯插畫風格。色調以淡藍 / 粉藍 / 天空藍為主，"
                "搭配一點奶油白與淺灰，整體感覺乾淨、療癒、討喜。"
                "意象明確、焦點集中，不要使用過於飽和或刺眼的顏色。"
                "整張圖不要出現任何文字或字符。"
            )
        return (
            "Cute, soft editorial illustration. Pastel sky-blue / baby-blue / cream "
            "palette, gentle and friendly, clean focal point. No harsh saturation, "
            "no text anywhere."
        )
    if chinese:
        return (
            "簡潔可愛的 Instagram 資訊卡片，方形 1:1 構圖。"
            "背景使用淡藍色（如 #DCEEFB / #BFE0F8 / 天空藍）或奶油白漸層，"
            "搭配深藍色（#1F4F7A）的標題與內文，"
            "重點處可以用一點粉藍或薄荷綠點綴，整體風格乾淨、可愛、討喜。"
            "邊距留白寬鬆，使用清晰、現代的繁體中文無襯線字型（例如思源黑體 / Noto Sans TC）。"
            "嚴禁出現任何簡體中文字，所有字皆需為繁體中文。"
            "嚴禁使用紅色 / 黃色 / 黑底等對比過強的配色。"
        )
    return (
        "Cute, clean Instagram infographic card, square 1:1. "
        "Soft light-blue background (#DCEEFB / #BFE0F8 / sky-blue) or cream gradient, "
        "deep-blue (#1F4F7A) title and body text, optional mint or pastel-blue accents. "
        "Generous padding, modern friendly sans-serif typography, no harsh contrast."
    )


def _signature_text(config: dict) -> str:
    """Return the signature line to render at the bottom of every card, or ''."""
    image_cfg = config.get("image_generation", {})
    sig = image_cfg.get("signature")
    if sig:
        return str(sig).strip()
    # Auto-derive from Instagram config when posting is enabled
    ig_cfg = config.get("instagram", {})
    if ig_cfg.get("enabled"):
        handle = ig_cfg.get("handle")
        if handle:
            return f"ig:{str(handle).lstrip('@').strip()}"
    return ""


# ---------------------------------------------------------------------------
# Public image-hosting fallback chain
# ---------------------------------------------------------------------------
#
# OpenAI's gpt-image-* family returns base64 only, but Instagram's Graph API
# requires a publicly-fetchable HTTPS URL. We bridge that gap by uploading
# the saved PNG to a free anonymous image host and using the returned URL.
#
# No single free host is reliable enough on its own:
#   - catbox.moe occasionally responds 200 OK while storing a 0-byte file,
#     and dedupes by content hash so retrying the same bytes returns the
#     same broken URL
#   - uguu.se uploads work but Instagram rejects URLs from this domain
#   - 0x0.st is currently disabled by the operator (anti-abuse pause)
#
# The defence is a chain of hosts:
#   1. each host gets a small number of attempts with exponential backoff
#   2. every returned URL is verified with a GET that checks for non-empty
#      content (catches catbox's silent 0-byte failures)
#   3. on any failure we move on to the next host immediately
#   4. when every host fails we return None so the caller can skip Instagram
#      for that single image instead of erroring out the whole pipeline
#
# To add a new host: write `_upload_<name>(path) -> url` (raise on failure)
# and append it to `_HOSTS`. Order = priority.


def _verify_url_content(url: str, expected_min_bytes: int = 1024) -> bool:
    """Return True iff `url` resolves to image content of at least the given size."""
    try:
        r = requests.get(url, timeout=30)
        if not r.ok:
            return False
        ctype = r.headers.get("Content-Type", "").lower()
        # Reject HTML viewer pages that some hosts (tmpfiles) serve at the
        # canonical URL while keeping the actual binary at a `/dl/...` path.
        if "image" not in ctype and len(r.content) < 4 * expected_min_bytes:
            return False
        return len(r.content) >= expected_min_bytes
    except Exception:
        return False


def _upload_tmpfiles(image_path: Path) -> str | None:
    """tmpfiles.org — anonymous, ~60 min retention, accepted by Instagram.

    The viewer URL it returns serves an HTML page; the binary lives at
    `/dl/<id>/<name>`. We rewrite to the /dl/ path before returning.
    """
    with open(image_path, "rb") as fh:
        resp = requests.post(
            "https://tmpfiles.org/api/v1/upload",
            files={"file": (image_path.name, fh, "image/png")},
            timeout=120,
        )
    if not resp.ok:
        raise RuntimeError(f"status {resp.status_code}: {resp.text[:120]}")
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"unexpected response: {resp.text[:200]}")
    url = data["data"]["url"]
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    # /<id>/<name>  →  /dl/<id>/<name>
    url = url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
    return url


def _upload_catbox(image_path: Path) -> str | None:
    """catbox.moe — anonymous, persistent URLs, accepted by Instagram.

    Note: catbox dedupes by content hash, so if a previous identical
    upload was stored as 0 bytes the next upload of the same content
    returns that broken URL. The caller's verification step catches this.
    """
    with open(image_path, "rb") as fh:
        resp = requests.post(
            "https://catbox.moe/user/api.php",
            data={"reqtype": "fileupload"},
            files={"fileToUpload": fh},
            timeout=120,
        )
    if not resp.ok:
        raise RuntimeError(f"status {resp.status_code}: {resp.text[:120]}")
    url = resp.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"unexpected response: {resp.text[:120]}")
    return url


def _upload_uguu(image_path: Path) -> str | None:
    """uguu.se — anonymous, ~few hours retention.

    Useful as a last-resort fallback. Note: at the time of writing
    Instagram rejects image URLs hosted on uguu.se, so it's mainly
    here so the saved-image path still has a public URL even when
    the IG-accepted hosts are down.
    """
    with open(image_path, "rb") as fh:
        resp = requests.post(
            "https://uguu.se/upload",
            files={"files[]": (image_path.name, fh, "image/png")},
            timeout=120,
        )
    if not resp.ok:
        raise RuntimeError(f"status {resp.status_code}: {resp.text[:120]}")
    data = resp.json()
    files = data.get("files") or []
    if not files or not files[0].get("url"):
        raise RuntimeError(f"unexpected response: {resp.text[:200]}")
    return files[0]["url"]


# Host priority — most IG-friendly first, then persistent-URL options,
# then last-resort. Each tuple: (display name, uploader callable).
_HOSTS: list[tuple[str, "callable"]] = [
    ("tmpfiles.org", _upload_tmpfiles),
    ("catbox.moe", _upload_catbox),
    ("uguu.se", _upload_uguu),
]


def _upload_to_public_host(image_path: Path, attempts_per_host: int = 2) -> str | None:
    """Upload to a public host and return a verified, IG-compatible URL.

    Walks the host chain in `_HOSTS` order. For each host:
      - try up to `attempts_per_host` times with exponential backoff
      - verify the returned URL serves real image content (>= 1 KiB)
      - on any failure, move to the next host

    Returns None when every host fails so the caller skips Instagram
    for this image rather than aborting the rest of the carousel.
    """
    import time
    last_err = ""
    for host_name, uploader in _HOSTS:
        for attempt in range(1, attempts_per_host + 1):
            try:
                url = uploader(image_path)
            except Exception as exc:
                last_err = f"{host_name}: {type(exc).__name__}: {exc}"
            else:
                if url and _verify_url_content(url):
                    return url
                last_err = f"{host_name}: URL returned but content is empty / unreadable ({url})"

            if attempt < attempts_per_host:
                backoff = 2 * attempt
                print(
                    f"    {host_name} attempt {attempt}/{attempts_per_host} failed "
                    f"({last_err}) — retrying in {backoff}s"
                )
                time.sleep(backoff)
        print(f"    {host_name} unavailable — falling through")

    print(f"    WARNING: all image hosts failed for {image_path.name}: {last_err}")
    return None


def _build_section_prompt(
    config: dict,
    title: str,
    body: str,
    idx: int,
    total: int,
    folder_name: str,
) -> str:
    report_title = config.get("report_title", "Podcast Digest")
    date_range = _format_date_range(folder_name)
    image_cfg = config.get("image_generation", {})
    style = image_cfg.get("style", "infographic")
    template = image_cfg.get("prompt_template")
    prefix = _style_prefix(config)
    card_label = f"Card {idx} of {total}" if total > 1 else ""

    highlights = _highlights_from_body(body)
    themes = _themes_from_body(body)

    if template:
        return template.format(
            report_title=report_title,
            date_range=date_range,
            section_title=title,
            card_label=card_label,
            highlights=highlights,
            themes=themes,
        )

    chinese = _is_chinese(config)
    signature = _signature_text(config)

    if style == "illustration":
        if chinese:
            sig_zh = (
                f"在卡片右下角放一個小小的、低調的浮水印簽名「{signature}」，"
                "字體比主視覺小很多，顏色為半透明的中性灰，不要喧賓奪主。"
                if signature else ""
            )
            return (
                f"{'（' + card_label + '）' if card_label else ''}"
                f"為《{report_title}》（{date_range}）的「{title}」段落設計一張 Instagram 插畫。"
                f"{prefix} 主題內容：{themes} {sig_zh}"
            )
        sig_en = (
            f" Place a small, subtle watermark signature '{signature}' in the "
            "bottom-right corner — much smaller than the main art, semi-transparent grey."
            if signature else ""
        )
        return (
            f"{'(' + card_label + ') ' if card_label else ''}"
            f"Instagram illustration for '{report_title}' ({date_range}), "
            f"section '{title}'. {prefix} Themes: {themes}{sig_en}"
        )

    # Default: infographic summary card
    if chinese:
        zh_label = f"（{card_label}）" if card_label else ""
        sig_zh = (
            f"\n在卡片底部放一個小巧、低調的簽名「{signature}」，"
            "字體比內文小，置中或靠右對齊，使用淡藍色或柔和的灰色，"
            "不要搶主視覺的焦點。"
            if signature else ""
        )
        return (
            f"請為《{report_title}》（{date_range}）設計一張 Instagram 資訊卡片。\n"
            f"{zh_label}段落主題：{title}\n\n"
            f"請完整且清楚地呈現以下重點作為卡片主要內容（請逐條列出，全部使用繁體中文）：\n{highlights}\n\n"
            f"視覺風格：{prefix}\n"
            "排版：段落標題以粗體放在卡片頂部，每一個重點獨立成一行，"
            "字體在手機上要清晰易讀；不要使用裝飾性元素干擾文字閱讀，留白要充足。"
            f"{sig_zh}\n"
            "重要：所有文字必須是正確的繁體中文（不可出現簡體字、亂碼、英文亂譯）。"
        )

    sig_en = (
        f"\nFooter: place a small, subtle signature '{signature}' at the very bottom "
        "of the card — small font, soft blue or muted grey, centred or right-aligned."
        if signature else ""
    )
    return (
        f"Design an Instagram summary card for '{report_title}' ({date_range}).\n"
        f"{'(' + card_label + ') ' if card_label else ''}Section: {title}\n\n"
        f"Display exactly these points as the main content:\n{highlights}\n\n"
        f"Visual style: {prefix}\n"
        "Layout: bold section title at top, each point on its own line, "
        f"readable at mobile size. No decorative clutter — let the text breathe.{sig_en}"
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate(
    config: dict,
    summary: str,
    folder_name: str,
    report_dir: Path,
) -> list[tuple[str | None, Path]]:
    """Generate one image per report section and save them to report_dir.

    Returns a list of (image_url, local_path) — one tuple per section.
    image_url is the OpenAI-hosted URL (valid ~60 min) or None when only
    b64 data was returned (Instagram posting skips those entries).
    """
    image_cfg = config.get("image_generation", {})
    api_key = os.environ.get("OPENAI_API_KEY") or image_cfg.get("api_key", "")
    if not api_key:
        raise RuntimeError(
            "OpenAI API key not set.\n"
            "  Export OPENAI_API_KEY=<your-key>\n"
            "  or set image_generation.api_key in config.yaml."
        )

    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    model = image_cfg.get("model", "gpt-image-2")
    size = image_cfg.get("size", "1024x1024")
    quality = image_cfg.get("quality", "standard")
    max_images = int(image_cfg.get("max_images", 10))

    sections = _parse_sections(summary)
    if not sections:
        sections = [("Weekly Highlights", summary)]
    sections = sections[:max_images]

    print(f"  {len(sections)} section(s) → {len(sections)} image(s)  "
          f"[model={model}, size={size}, quality={quality}]")

    results: list[tuple[str | None, Path]] = []
    for idx, (title, body) in enumerate(sections, 1):
        prompt = _build_section_prompt(config, title, body, idx, len(sections), folder_name)
        # Keep word characters (incl. CJK) — only strip punctuation/whitespace.
        slug = re.sub(r"[^\w]+", "_", title, flags=re.UNICODE).strip("_").lower()
        if not slug:
            slug = f"section{idx}"
        image_path = report_dir / f"card_{idx}_{slug}.png"

        print(f"  [{idx}/{len(sections)}] {title}")
        print(f"    Prompt : {prompt[:100]}{'…' if len(prompt) > 100 else ''}")

        try:
            try:
                resp = client.images.generate(
                    model=model, prompt=prompt, size=size,
                    quality=quality, n=1, response_format="url",
                )
            except Exception:
                resp = client.images.generate(
                    model=model, prompt=prompt, size=size, quality=quality, n=1,
                )

            image_obj = resp.data[0]
            if getattr(image_obj, "url", None):
                # Older models (dall-e-2/3) return a hosted URL. Use requests
                # so we go through certifi — Python 3.14's stdlib urllib lacks
                # system CA certs on macOS.
                image_url: str | None = image_obj.url
                r = requests.get(image_url, timeout=60)
                r.raise_for_status()
                image_path.write_bytes(r.content)
            elif getattr(image_obj, "b64_json", None):
                # Newer models (gpt-image-1/2) only return base64.
                # Save locally then upload to a public host so Instagram can fetch it.
                image_path.write_bytes(base64.b64decode(image_obj.b64_json))
                image_url = _upload_to_public_host(image_path)
                if image_url:
                    print(f"    Hosted : {image_url}")
                else:
                    print("    NOTE: no public URL — Instagram will skip this image.")
            else:
                raise RuntimeError("No image data returned.")

            print(f"    Saved  : {image_path.name}")
            results.append((image_url, image_path))

        except Exception as exc:
            print(f"    WARNING: Failed — {exc}")

    return results
