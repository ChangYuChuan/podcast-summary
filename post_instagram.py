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
import time

import requests


def _format_date_range(folder_name: str) -> str:
    try:
        start, end = folder_name.split("-")
        return f"{start[:4]}/{start[4:6]}/{start[6:]} – {end[:4]}/{end[4:6]}/{end[6:]}"
    except Exception:
        return folder_name


def _build_caption(config: dict, folder_name: str) -> str:
    ig_cfg = config.get("instagram", {})
    report_title = config.get("report_title", "Podcast Digest")
    date_range = _format_date_range(folder_name)

    template = ig_cfg.get("caption_template")
    if template:
        return template.format(report_title=report_title, date_range=date_range)

    return (
        f"🎙 {report_title}\n"
        f"{date_range}\n\n"
        "Weekly podcast digest — full summary delivered to subscribers.\n"
        "#podcast #weekly #digest #ai"
    )


def _check_response(resp: requests.Response, action: str) -> None:
    if resp.ok:
        return
    try:
        error = resp.json().get("error", {})
        msg = error.get("message", resp.text)
    except Exception:
        msg = resp.text
    raise RuntimeError(f"Instagram API error during {action}: {msg}")


def _wait_for_container(
    base_url: str,
    container_id: str,
    access_token: str,
    max_attempts: int = 12,
    interval: float = 5.0,
) -> None:
    """Poll until the media container status is FINISHED or raise on ERROR/timeout."""
    for attempt in range(max_attempts):
        resp = requests.get(
            f"{base_url}/{container_id}",
            params={"fields": "status_code", "access_token": access_token},
            timeout=30,
        )
        _check_response(resp, "poll container status")
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
    resp = requests.post(
        f"{base_url}/{user_id}/media",
        params={"image_url": image_url, "caption": caption, "access_token": access_token},
        timeout=30,
    )
    _check_response(resp, "create media container")
    container_id = resp.json()["id"]
    print(f"  Container ID : {container_id}")

    _wait_for_container(base_url, container_id, access_token)

    print("  Publishing …")
    resp = requests.post(
        f"{base_url}/{user_id}/media_publish",
        params={"creation_id": container_id, "access_token": access_token},
        timeout=30,
    )
    _check_response(resp, "publish media")
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
        resp = requests.post(
            f"{base_url}/{user_id}/media",
            params={
                "image_url": url,
                "is_carousel_item": "true",
                "access_token": access_token,
            },
            timeout=30,
        )
        _check_response(resp, f"create carousel child {i}")
        child_ids.append(resp.json()["id"])

    # Step 2 — Create the carousel container referencing all children
    print("  Creating carousel container …")
    resp = requests.post(
        f"{base_url}/{user_id}/media",
        params={
            "media_type": "CAROUSEL",
            "children": ",".join(child_ids),
            "caption": caption,
            "access_token": access_token,
        },
        timeout=30,
    )
    _check_response(resp, "create carousel container")
    carousel_id = resp.json()["id"]
    print(f"  Carousel ID : {carousel_id}")

    # Step 3 — Wait for the carousel container to be ready
    _wait_for_container(base_url, carousel_id, access_token)

    # Step 4 — Publish
    print("  Publishing carousel …")
    resp = requests.post(
        f"{base_url}/{user_id}/media_publish",
        params={"creation_id": carousel_id, "access_token": access_token},
        timeout=30,
    )
    _check_response(resp, "publish carousel")
    media_id = resp.json()["id"]
    print(f"  Published! Media ID : {media_id}")
    return media_id


def _resolve_user_id(base_url: str, access_token: str, ig_cfg: dict) -> str:
    """Return the Instagram user ID from config, or auto-discover it via GET /me."""
    user_id = ig_cfg.get("user_id", "").strip()
    if user_id:
        return user_id
    print("  Resolving Instagram user ID from token …")
    resp = requests.get(
        f"{base_url}/me",
        params={"fields": "id,username", "access_token": access_token},
        timeout=30,
    )
    _check_response(resp, "resolve user ID")
    data = resp.json()
    user_id = data.get("id", "")
    if not user_id:
        raise RuntimeError(
            "Could not resolve Instagram user ID from token. "
            "Set instagram.user_id in config.yaml as a fallback."
        )
    print(f"  User ID : {user_id}  (@{data.get('username', '?')})")
    return user_id


def post(config: dict, image_urls: list[str], folder_name: str) -> str:
    """Post images to Instagram. Returns the published media ID.

    Automatically selects single-image or carousel based on the number of URLs.
    The Instagram user ID is read from config; if absent it is auto-discovered
    from the access token via GET /me.
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
    caption = _build_caption(config, folder_name)

    if len(image_urls) == 1:
        print(f"  Posting single image …")
        return _post_single(base_url, user_id, access_token, image_urls[0], caption)

    print(f"  Posting carousel ({len(image_urls)} images) …")
    return _post_carousel(base_url, user_id, access_token, image_urls, caption)
