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

    The report format produced by query_all_sections() is:
        ## Title\\n\\n<body>\\n\\n---\\n\\n## Title\\n\\n<body>…
    """
    sections = []
    for block in summary.split("\n\n---\n\n"):
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        title = lines[0].lstrip("#").strip()
        body = "\n".join(lines[1:]).strip()
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

def _style_prefix(config: dict) -> str:
    """Shared visual-style description applied to every image in the set.

    Being identical across all prompts is what makes the generated cards look
    like a cohesive series rather than unrelated images.
    """
    image_cfg = config.get("image_generation", {})
    if image_cfg.get("style_base"):
        return image_cfg["style_base"]
    if image_cfg.get("style", "infographic") == "illustration":
        return (
            "Editorial magazine illustration. Vibrant saturated colours, bold conceptual "
            "imagery, strong focal point. No text or words anywhere."
        )
    return (
        "Clean Instagram infographic card. Minimal background (white or light cream), "
        "one accent colour, generous padding, bold readable sans-serif typography, "
        "square (1:1) format."
    )


def _upload_to_public_host(image_path: Path, attempts: int = 3) -> str | None:
    """Upload an image file to a public host and return the URL.

    Bridges base64-only models (gpt-image-1, gpt-image-2) to Instagram's
    Graph API, which requires a publicly-fetchable image URL — it cannot
    accept binary or base64 data directly.

    Uses catbox.moe (no auth, files persist). Retries on timeout/transient
    failure since the host occasionally rejects requests under load.
    Returns None on permanent failure so the caller can decide whether
    to skip Instagram for that image.
    """
    import time
    last_err: str = ""
    for attempt in range(1, attempts + 1):
        try:
            with open(image_path, "rb") as fh:
                resp = requests.post(
                    "https://catbox.moe/user/api.php",
                    data={"reqtype": "fileupload"},
                    files={"fileToUpload": fh},
                    timeout=120,
                )
            if resp.ok:
                url = resp.text.strip()
                if url.startswith("http"):
                    return url
            last_err = f"status {resp.status_code}: {resp.text[:120]}"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"

        if attempt < attempts:
            backoff = 2 * attempt
            print(f"    Catbox attempt {attempt}/{attempts} failed ({last_err}) — retrying in {backoff}s")
            time.sleep(backoff)

    print(f"    WARNING: catbox upload failed after {attempts} attempt(s): {last_err}")
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

    if style == "illustration":
        return (
            f"{'(' + card_label + ') ' if card_label else ''}"
            f"Instagram illustration for '{report_title}' ({date_range}), "
            f"section '{title}'. {prefix} Themes: {themes}"
        )

    # Default: infographic summary card
    return (
        f"Design an Instagram summary card for '{report_title}' ({date_range}).\n"
        f"{'(' + card_label + ') ' if card_label else ''}Section: {title}\n\n"
        f"Display exactly these points as the main content:\n{highlights}\n\n"
        f"Visual style: {prefix}\n"
        "Layout: bold section title at top, each point on its own line, "
        "readable at mobile size. No decorative clutter — let the text breathe."
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
        slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
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
