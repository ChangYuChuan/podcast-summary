from __future__ import annotations
"""
post_instagram.py

Stage 6 (optional): Post generated images to Instagram using the Instagram
Graph API.

  - 1 image  → standard single-image post
  - 2+ images → carousel post (one swipeable post, up to 10 images)

Requirements:
  - An Instagram Business or Creator account linked to a Facebook App
  - A long-lived User Access Token with instagram_basic and
    instagram_content_publish permissions
  - INSTAGRAM_ACCESS_TOKEN environment variable (or instagram.access_token in config)

Config keys:
  instagram:
    enabled: true
    user_id: "12345678"         # optional — auto-discovered from the token via GET /me
    api_version: v21.0          # Graph API version (default: v21.0)
    caption_template: "..."     # optional; placeholders: {report_title} {date_range}

The only hard requirement is the access token. The user ID is resolved automatically
if not set. All image URLs must be publicly accessible — the OpenAI-hosted URLs
(valid ~60 min) produced by generate_image.py are used automatically.
"""

import os
import re
import time

import requests


# Instagram caption hard cap is 2,200 chars; keep well below to leave headroom.
MAX_CAPTION_CHARS = 2_000

# Per-request timeout. Container creation involves Instagram's servers
# fetching the image from our host (e.g. tmpfiles.org) which can be slow,
# so be generous on the upload-y endpoints.
IG_REQUEST_TIMEOUT = 90

# Total retries for transient failures (timeouts, 5xx, connection drops)
# on each Graph API call. Doesn't retry permanent errors like 4xx.
IG_RETRY_ATTEMPTS = 3


def _format_date_range(folder_name: str) -> str:
    try:
        start, end = folder_name.split("-")
        return f"{start[:4]}/{start[4:6]}/{start[6:]} – {end[:4]}/{end[4:6]}/{end[6:]}"
    except Exception:
        return folder_name


def _is_chinese(config: dict) -> bool:
    lang = config.get("instagram", {}).get("language") or config.get(
        "whisper_language", "en"
    )
    return lang.lower().startswith("zh")


def _section_blocks(summary: str) -> list[tuple[str, str]]:
    """Split the report into [(title, body)] pairs.

    Splits on `## ` markdown headers (top-level section markers from
    send_report.query_all_sections) instead of `---` separators —
    NotebookLM answers contain internal horizontal rules that would
    otherwise chop one section into many.
    """
    pattern = re.compile(r"^## +(.+?)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(summary))
    blocks: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(summary)
        body = re.sub(r"(\s*\n\s*-{3,}\s*)+\s*$", "", summary[start:end]).strip()
        if title and body:
            blocks.append((title, body))
    return blocks


# Safety filters for caption bullets — even with prompt-level scoping, the
# model occasionally slips through reported-speech phrasing or off-topic bullets.
_REPORTED_SPEECH = re.compile(
    r"主持人(認為|提到|表示|覺得|建議|指出)|"
    r"節目中(提到|表示|指出)|"
    r"他們(認為|表示|覺得)|"
    r"分析師(覺得|認為)"
)
_OFF_TOPIC_HINTS = re.compile(
    r"(日常生活|生活雜談|美食|旅遊|3C 開箱|AI 工具教學|運動|娛樂|八卦)"
)


def _strip_reported_speech(text: str) -> str:
    """Drop common '主持人 認為 / 節目中 提到' wrapper from the start of a bullet."""
    return re.sub(
        r"^(主持人(認為|提到|表示|覺得|建議|指出)|"
        r"節目中(提到|表示|指出)|"
        r"他們(認為|表示|覺得)|"
        r"分析師(覺得|認為))[，：、]?\s*",
        "",
        text,
    )


def _bullets(body: str, max_items: int) -> list[str]:
    """Pull up to max_items bullet-style lines from a section body, cleaned.

    Skips lines that look like off-topic (daily-life / entertainment) noise
    and trims away reported-speech wrappers like '主持人認為...'.
    """
    items: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        m = re.match(r"^[-*•]\s+(.+)$", line) or re.match(r"^\d+\.\s+(.+)$", line)
        if not m:
            continue
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(1)).strip()
        text = re.sub(r"\s+", " ", text)
        if _OFF_TOPIC_HINTS.search(text):
            continue
        text = _strip_reported_speech(text)
        if len(text) > 110:
            text = text[:107] + "…"
        if text:
            items.append(text)
        if len(items) >= max_items:
            break
    return items


def _looks_like_stocks_section(title: str) -> bool:
    t = title.lower()
    return (
        "stock" in t
        or "ticker" in t
        or "個股" in title
        or "股票" in title
        or "推薦" in title
    )


def _stocks_lines(summary: str, max_items: int = 8) -> list[str]:
    """Pull bullet lines from the 'Stocks Mentioned' / '本期提到的個股' section, if any."""
    for title, body in _section_blocks(summary):
        if _looks_like_stocks_section(title):
            return _bullets(body, max_items)
    return []


def _highlight_lines(summary: str, per_section: int = 2, total: int = 6) -> list[str]:
    """Pull a few bullets from non-stocks sections to give the caption substance."""
    items: list[str] = []
    for title, body in _section_blocks(summary):
        if _looks_like_stocks_section(title):
            continue
        for b in _bullets(body, per_section):
            items.append(b)
            if len(items) >= total:
                return items
    return items


def _trim_caption(text: str) -> str:
    if len(text) <= MAX_CAPTION_CHARS:
        return text
    return text[: MAX_CAPTION_CHARS - 1].rstrip() + "…"


# Subjective / recommendation language — stripped from caption one-liners so
# the post stays neutral. Lines that are *just* a stance phrase get skipped;
# lines where it's a prefix get the prefix stripped.
_STANCE_TERMS = (
    "看多", "看空", "看好", "不看好", "看淡", "中性", "觀察", "中立",
    "整體看法", "看法",
    "建議買進", "建議賣出", "建議布局", "進場", "出場", "逢低買進", "值得布局",
    "Overall view", "View", "Bullish", "Bearish",
)
_STANCE_PREFIX_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(t) for t in _STANCE_TERMS) + r")\s*[：:、，,。.\-—]*\s*"
)


def _looks_like_stance_only(text: str) -> bool:
    stripped = text.strip().rstrip("。.！!").rstrip("，,、")
    return any(stripped == t or stripped == t + "。" for t in _STANCE_TERMS)


def _strip_stance_prefix(text: str) -> str:
    return _STANCE_PREFIX_RE.sub("", text).strip()


def _stock_oneliner(body: str) -> str:
    """Pull a neutral, factual one-liner out of a per-stock section body.

    Skips bullets that are pure stance / recommendation language and trims
    any stance prefix off the remaining ones. After stripping we re-check
    that what's left isn't itself just a stance term (covers cases like
    "看法：看多" → strip "看法：" → "看多" which we should skip too).
    """
    bullets = _bullets(body, max_items=8)
    for b in bullets:
        if _looks_like_stance_only(b):
            continue
        text = _strip_stance_prefix(b).strip()
        if not text or _looks_like_stance_only(text) or len(text) < 5:
            continue
        return text[:90]
    return ""


def _stocks_mode_blocks(summary: str | None) -> list[tuple[str, str]]:
    """In stocks mode, every section is one stock — title is the stock name."""
    if not summary:
        return []
    return _section_blocks(summary)


def _build_caption(config: dict, folder_name: str, summary: str | None) -> str:
    ig_cfg = config.get("instagram", {})
    report_title = config.get("report_title", "Podcast Digest")
    date_range = _format_date_range(folder_name)
    chinese = _is_chinese(config)
    discover_mode = config.get("report_mode") in ("discover", "stocks")

    template = ig_cfg.get("caption_template")
    if template:
        return _trim_caption(
            template.format(report_title=report_title, date_range=date_range)
        )

    # ── Discover mode: caption is a roll-call of the items covered ──────────
    if discover_mode:
        discovery_cfg = config.get("discovery") or {}
        instagram_cfg = config.get("instagram") or {}

        blocks = _stocks_mode_blocks(summary)
        if chinese:
            heading = (
                discovery_cfg.get("caption_heading_zh")
                or instagram_cfg.get("caption_heading_zh")
                or "本期重點個股"
            )
            hashtags = (
                instagram_cfg.get("hashtags_zh")
                or "#股市 #投資 #台股 #美股 #個股分析 #財經"
            )
            parts = [f"🎙《{report_title}》", f"📅 {date_range}", ""]
            if blocks:
                parts.append(f"📈 {heading}")
                for item_name, body in blocks:
                    one = _stock_oneliner(body)
                    parts.append(f"• {item_name} — {one}" if one else f"• {item_name}")
                parts.append("")
            parts.append(hashtags)
            return _trim_caption("\n".join(parts).rstrip())

        heading = (
            discovery_cfg.get("caption_heading_en")
            or instagram_cfg.get("caption_heading_en")
            or "Items covered"
        )
        hashtags = (
            instagram_cfg.get("hashtags_en")
            or "#digest #investing #stocks #markets"
        )
        parts = [f"🎙 {report_title}", f"📅 {date_range}", ""]
        if blocks:
            parts.append(heading)
            for item_name, body in blocks:
                one = _stock_oneliner(body)
                parts.append(f"• {item_name} — {one}" if one else f"• {item_name}")
            parts.append("")
        parts.append(hashtags)
        return _trim_caption("\n".join(parts).rstrip())

    # ── Sections mode (default, unchanged) ─────────────────────────────────
    stocks = _stocks_lines(summary) if summary else []
    highlights = _highlight_lines(summary) if summary else []

    if chinese:
        parts = [
            f"🎙《{report_title}》",
            f"📅 {date_range}",
            "",
        ]
        if highlights:
            parts.append("📝 本期重點")
            parts.extend(f"• {h}" for h in highlights)
            parts.append("")
        if stocks:
            parts.append("📈 本期提到的個股")
            parts.extend(f"• {s}" for s in stocks)
            parts.append("")
        parts.append("#股市 #投資 #台股 #美股 #財經")
        return _trim_caption("\n".join(parts).rstrip())

    parts = [
        f"🎙 {report_title}",
        f"📅 {date_range}",
        "",
    ]
    if highlights:
        parts.append("Highlights")
        parts.extend(f"• {h}" for h in highlights)
        parts.append("")
    if stocks:
        parts.append("Stocks mentioned")
        parts.extend(f"• {s}" for s in stocks)
        parts.append("")
    parts.append("#digest #investing #stocks")
    return _trim_caption("\n".join(parts).rstrip())


def _check_response(resp: requests.Response, action: str) -> None:
    if resp.ok:
        return
    try:
        error = resp.json().get("error", {})
        msg = error.get("message", resp.text)
    except Exception:
        msg = resp.text
    raise RuntimeError(f"Instagram API error during {action}: {msg}")


def _ig_request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    action: str,
    timeout: int = IG_REQUEST_TIMEOUT,
    attempts: int = IG_RETRY_ATTEMPTS,
) -> requests.Response:
    """Wrap a Graph API call with retry on transient errors.

    Retries on connection errors / read timeouts (network) and 5xx
    responses. Permanent client-side errors (4xx) are raised immediately
    via _check_response so we don't hammer the API on a bad request.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.request(method, url, params=params, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt < attempts:
                backoff = 5 * attempt
                print(f"    {action}: {type(exc).__name__} — retry {attempt}/{attempts - 1} in {backoff}s")
                time.sleep(backoff)
            continue

        if resp.ok:
            return resp
        # Retry server-side errors; surface client errors immediately
        if 500 <= resp.status_code < 600 and attempt < attempts:
            backoff = 5 * attempt
            print(f"    {action}: HTTP {resp.status_code} — retry {attempt}/{attempts - 1} in {backoff}s")
            time.sleep(backoff)
            last_exc = RuntimeError(f"HTTP {resp.status_code}")
            continue
        _check_response(resp, action)  # raises

    raise RuntimeError(
        f"Instagram API error during {action}: exhausted {attempts} attempts ({last_exc})"
    )


def _wait_for_container(
    base_url: str,
    container_id: str,
    access_token: str,
    max_attempts: int = 12,
    interval: float = 5.0,
) -> None:
    """Poll until the media container status is FINISHED or raise on ERROR/timeout."""
    for attempt in range(max_attempts):
        resp = _ig_request(
            "GET",
            f"{base_url}/{container_id}",
            params={"fields": "status_code", "access_token": access_token},
            action="poll container status",
            timeout=30,
        )
        status = resp.json().get("status_code", "IN_PROGRESS")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError("Instagram media container failed with status: ERROR")
        print(f"    Container status: {status} (attempt {attempt + 1}/{max_attempts}) …")
        time.sleep(interval)
    raise RuntimeError("Instagram media container did not become ready in time.")


def _post_single(
    base_url: str, user_id: str, access_token: str, image_url: str, caption: str
) -> str:
    """Create a single-image post and return the published media ID."""
    print("  Creating media container …")
    resp = _ig_request(
        "POST",
        f"{base_url}/{user_id}/media",
        params={"image_url": image_url, "caption": caption, "access_token": access_token},
        action="create media container",
    )
    container_id = resp.json()["id"]
    print(f"  Container ID : {container_id}")

    _wait_for_container(base_url, container_id, access_token)

    print("  Publishing …")
    resp = _ig_request(
        "POST",
        f"{base_url}/{user_id}/media_publish",
        params={"creation_id": container_id, "access_token": access_token},
        action="publish media",
    )
    media_id = resp.json()["id"]
    print(f"  Published! Media ID : {media_id}")
    return media_id


def _post_carousel(
    base_url: str, user_id: str, access_token: str, image_urls: list[str], caption: str
) -> str:
    """Create a carousel post from multiple image URLs and return the published media ID."""
    # Step 1 — Create a child container for each image
    child_ids: list[str] = []
    for i, url in enumerate(image_urls, 1):
        print(f"  Creating child container {i}/{len(image_urls)} …")
        resp = _ig_request(
            "POST",
            f"{base_url}/{user_id}/media",
            params={
                "image_url": url,
                "is_carousel_item": "true",
                "access_token": access_token,
            },
            action=f"create carousel child {i}",
        )
        child_ids.append(resp.json()["id"])

    # Step 2 — Create the carousel container referencing all children
    print("  Creating carousel container …")
    resp = _ig_request(
        "POST",
        f"{base_url}/{user_id}/media",
        params={
            "media_type": "CAROUSEL",
            "children": ",".join(child_ids),
            "caption": caption,
            "access_token": access_token,
        },
        action="create carousel container",
    )
    carousel_id = resp.json()["id"]
    print(f"  Carousel ID : {carousel_id}")

    # Step 3 — Wait for the carousel container to be ready
    _wait_for_container(base_url, carousel_id, access_token)

    # Step 4 — Publish
    print("  Publishing carousel …")
    resp = _ig_request(
        "POST",
        f"{base_url}/{user_id}/media_publish",
        params={"creation_id": carousel_id, "access_token": access_token},
        action="publish carousel",
    )
    media_id = resp.json()["id"]
    print(f"  Published! Media ID : {media_id}")
    return media_id


def _resolve_user_id(base_url: str, access_token: str, ig_cfg: dict) -> str:
    """Return the Instagram user ID from config, or auto-discover it via GET /me."""
    user_id = ig_cfg.get("user_id", "").strip()
    if user_id:
        return user_id
    print("  Resolving Instagram user ID from token …")
    resp = _ig_request(
        "GET",
        f"{base_url}/me",
        params={"fields": "id,username", "access_token": access_token},
        action="resolve user ID",
        timeout=30,
    )
    data = resp.json()
    user_id = data.get("id", "")
    if not user_id:
        raise RuntimeError(
            "Could not resolve Instagram user ID from token. "
            "Set instagram.user_id in config.yaml as a fallback."
        )
    print(f"  User ID : {user_id}  (@{data.get('username', '?')})")
    return user_id


def post(
    config: dict,
    image_urls: list[str],
    folder_name: str,
    summary: str | None = None,
) -> str:
    """Post images to Instagram. Returns the published media ID.

    Automatically selects single-image or carousel based on the number of URLs.
    The Instagram user ID is read from config; if absent it is auto-discovered
    from the access token via GET /me.

    When summary is provided, the caption is built from the report's actual
    content (highlights + stocks-mentioned section) instead of a generic
    boilerplate.
    """
    ig_cfg = config.get("instagram", {})

    access_token = (
        os.environ.get("INSTAGRAM_ACCESS_TOKEN") or ig_cfg.get("access_token", "")
    )
    if not access_token:
        raise RuntimeError(
            "Instagram access token not set.\n"
            "  Export INSTAGRAM_ACCESS_TOKEN=<your-long-lived-token>\n"
            "  or set instagram.access_token in config.yaml."
        )

    if not image_urls:
        raise RuntimeError("No image URLs provided to post.")

    api_version = ig_cfg.get("api_version", "v21.0")
    base_url = f"https://graph.instagram.com/{api_version}"
    user_id = _resolve_user_id(base_url, access_token, ig_cfg)
    caption = _build_caption(config, folder_name, summary)

    if len(image_urls) == 1:
        print(f"  Posting single image …")
        return _post_single(base_url, user_id, access_token, image_urls[0], caption)

    print(f"  Posting carousel ({len(image_urls)} images) …")
    return _post_carousel(base_url, user_id, access_token, image_urls, caption)
