from __future__ import annotations
"""
send_report.py

Stage 4: Generate a weekly insight report from a NotebookLM notebook
and email it to the configured recipient.

Steps:
  1. Run `nlm report create` to create a Briefing Doc inside the notebook
  2. Run `nlm query notebook` to capture a text summary for the email body
  3. Send the summary + notebook link via SMTP

SMTP password is read from the EMAIL_SMTP_PASSWORD environment variable.
If not set, it falls back to smtp_password in config.yaml (not recommended
for production — prefer the env var so credentials stay out of the repo).

Usage (standalone):
  python send_report.py --notebook-id <id>
  python send_report.py --notebook-id <id> --folder 20260218-20260225
  python send_report.py --config my_config.yaml --notebook-id <id>
"""

import argparse
import json
import os
import re
import smtplib
import subprocess
import sys
import yaml
import markdown as md
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# A meaningful weekly summary should comfortably exceed this.
# If NotebookLM returns less, something went wrong and we should not send it.
MIN_REPORT_CHARS = 5_000

# ---------------------------------------------------------------------------
# Default report sections — each is queried independently so we are not
# limited by a single response's length cap.
#
# Override these in config.yaml via the `report_sections` key:
#   report_sections:
#     - title: "Section Title"
#       prompt: "The question / prompt to ask NotebookLM for this section"
# ---------------------------------------------------------------------------
REPORT_SECTIONS = [
    (
        "Episode Summaries",
        (
            "Please provide a detailed summary of each podcast episode covered this week. "
            "For each episode include: the main topic, key arguments, notable facts or data "
            "mentioned, and any conclusions drawn by the host(s). "
            "Label each episode clearly with its title and publication date."
        ),
    ),
    (
        "Key Themes & Insights",
        (
            "Identify and analyse the major themes, trends, and insights that emerged across "
            "all episodes this week. Highlight any points of consensus or disagreement between "
            "hosts, and explain the significance of each theme for the listener."
        ),
    ),
    (
        "Stocks Mentioned",
        (
            "List every individual stock, ticker, ETF, or company mentioned across all "
            "episodes this period. For each: include the ticker / Chinese name, the host's "
            "view (bullish / bearish / neutral / watch), the key reason given, and which "
            "episode it came from. Group bullish, bearish, and watch-list ideas separately. "
            "Only include securities the hosts actually discussed — do not invent names."
        ),
    ),
    (
        "Takeaways & Action Items",
        (
            "List the most important takeaways and any concrete recommendations or action items "
            "mentioned across all episodes this week. Organise them by topic or priority."
        ),
    ),
]


# Traditional Chinese default sections — used automatically when
# whisper_language is "zh" / "zh-TW" / "zh-Hant" and no custom
# report_sections are provided in config.
_ZH_SCOPE = (
    "範圍限定：只包含與科技、AI、股市、個股、產業、總體經濟、央行 / 利率政策、外匯、商品、"
    "投資策略相關的內容。請忽略並排除任何與此無關的話題，例如：日常生活、娛樂、"
    "旅遊、美食、運動、人物八卦、3C 開箱、生活雜談等。"
)

_ZH_VOICE = (
    "請以直接陳述事實的方式撰寫，例如「Intel 的 IDM 2.0 策略已被驗證，"
    "並且受惠於 Agentic AI 帶動的 CPU 需求」，"
    "不要使用「主持人」、「節目中提到」、「他們認為」、「分析師覺得」這類轉述語句。"
)

_ZH_COMPLETE = (
    "請以完整、自包含的句子作答，不要在句中或論點中途斷掉。"
    "每一個重點都應該有清楚的主詞、動詞、結論或結果（例如「主因在於 X，因此 Y」），"
    "不可以只寫「主因在於...」或「主要原因是...」這類沒有結尾的片段。"
    "答覆中不要保留 [1]、[1-3]、[1, 2] 這類引用標記。"
)

REPORT_SECTIONS_ZH = [
    (
        "昨日股市摘要",
        (
            "請針對昨日的每一集節目，提供與投資 / 財經主題相關的詳細摘要。"
            f"{_ZH_SCOPE} "
            "每一集請包含：主要主題、核心論點、重要事實或數據、以及結論。"
            f"{_ZH_VOICE} {_ZH_COMPLETE} "
            "請標明每一集的節目名稱與發佈日期。如果某一集完全沒有相關內容，"
            "請直接寫「本集無相關內容」。請使用繁體中文回答。"
        ),
    ),
    (
        "重點主題與洞察",
        (
            "請辨識並分析昨日所有節目中浮現的主要市場主題、產業趨勢與投資洞察。"
            f"{_ZH_SCOPE} "
            "若不同節目之間有共識或分歧，請特別標註，並說明每個主題對投資人的意義。"
            f"{_ZH_VOICE} {_ZH_COMPLETE} 請使用繁體中文回答。"
        ),
    ),
    (
        "昨日提到的個股",
        (
            "請列出昨日所有節目中被提到的每一檔個股、ETF 或公司。"
            "針對每一檔，請包含：股票代號 / 中文名稱、看法（看多 / 看空 / 中性 / 觀察）、"
            "核心理由（基本面 / 技術面 / 籌碼面）、以及來自哪一集節目。"
            "請將看多、看空、觀察名單分組呈現。"
            "只列出節目實際討論過的標的，不要憑空產生。"
            f"{_ZH_VOICE} {_ZH_COMPLETE} "
            "例如完整寫「核心理由：IDM 2.0 策略已被驗證可行，且 A16 製程進度順利、獲特斯拉採用，"
            "因此產能利用率與毛利率有望同步提升」，而不是「主因在於 IDM 2.0...」這類斷句。"
            "請使用繁體中文回答。"
        ),
    ),
    (
        "重點結論與行動建議",
        (
            "請整理昨日所有節目中最重要的市場結論，"
            "以及與股市 / 投資 / 資產配置相關的具體建議或行動方案。"
            f"{_ZH_SCOPE} "
            "請依主題或優先順序分組。"
            f"{_ZH_VOICE} {_ZH_COMPLETE} 請使用繁體中文回答。"
        ),
    ),
]


def _is_chinese(lang: str) -> bool:
    return lang.lower().startswith("zh")


# ---------------------------------------------------------------------------
# Discover mode — one section per individual item the sources discussed.
# Activated by `report_mode: discover` in the config.
#
# Required config:
#   prompts:
#     discovery: |
#       <NotebookLM query that returns a numbered list of items>
#     detail: |
#       <per-item NotebookLM query — must contain {item} placeholder>
#
# No prompt content lives in the code — the config is the single source of
# truth. Missing prompts raise a clear RuntimeError at run time.
# ---------------------------------------------------------------------------

# Numbered-list parser for the discovery answer. Accepts the common bullet
# forms NotebookLM emits:  "1. Intel"   "2、 台積電"   "3) NVIDIA"
_DISCOVERY_LINE_PATTERN = re.compile(r"^\s*\d+\s*[.、)）]\s*(.+?)\s*$", re.MULTILINE)


def _resolve_discovery_prompt(config: dict) -> str:
    p = (config.get("prompts") or {}).get("discovery")
    if not p:
        raise RuntimeError(
            "Config is missing prompts.discovery — required when report_mode: discover. "
            "Add a `prompts.discovery: |` block to the config."
        )
    return p


def _resolve_detail_template(config: dict) -> str:
    t = (config.get("prompts") or {}).get("detail")
    if not t:
        raise RuntimeError(
            "Config is missing prompts.detail — required when report_mode: discover. "
            "Add a `prompts.detail: |` block to the config."
        )
    if "{item}" not in t:
        raise RuntimeError(
            "prompts.detail must contain a {item} placeholder so each discovered "
            "item can be substituted into the per-item query."
        )
    return t


def _discover_items(nlm_path: str, notebook_id: str, config: dict) -> list[str]:
    """Run the discovery query and return a deduped, ordered list of items."""
    raw = query_notebook(nlm_path, notebook_id, _resolve_discovery_prompt(config))
    candidates = [m.group(1).strip() for m in _DISCOVERY_LINE_PATTERN.finditer(raw)]
    seen, ordered = set(), []
    for c in candidates:
        c = re.sub(r"^\*+|\*+$", "", c).strip()
        key = c.lower()
        if key and key not in seen:
            seen.add(key)
            ordered.append(c)
    return ordered


def query_per_item_sections(nlm_path: str, notebook_id: str, config: dict) -> str:
    """Discover-mode report: one ## section per item the sources discussed.

    Both the discovery prompt and per-item detail template are read from
    `config.prompts` — see _resolve_discovery_prompt / _resolve_detail_template.

    Caps the number of items at `image_generation.max_images` (default 10)
    so the resulting image set stays within Instagram's carousel limit.
    """
    print("  Discovering items discussed this period …")
    items = _discover_items(nlm_path, notebook_id, config)
    cap = int(config.get("image_generation", {}).get("max_images", 10))
    items = items[:cap]
    if not items:
        print("  No items discovered. Falling back to a single placeholder section.")
        return "## 本期觀察\n\n資料來源中未明確討論到任何具體標的，請開啟 NotebookLM 筆記本檢視內容。"

    detail_template = _resolve_detail_template(config)
    print(f"  → {len(items)} item(s): {', '.join(items)}")
    parts: list[str] = []
    for idx, item in enumerate(items, start=1):
        print(f"  [{idx}/{len(items)}] Querying: {item} …")
        prompt = detail_template.format(item=item)
        try:
            answer = query_notebook(nlm_path, notebook_id, prompt)
        except RuntimeError as exc:
            print(f"    WARNING: query failed — {exc}")
            answer = "(此項目查詢失敗，請改開啟 NotebookLM 筆記本檢視。)"
        parts.append(f"## {item}\n\n{answer}")
        print(f"    → {len(answer):,} chars")
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Report sections helper
# ---------------------------------------------------------------------------

def _get_report_sections(config: dict) -> list[tuple[str, str]]:
    """Return report sections from config if defined, else use built-in defaults.

    The default template is selected by `whisper_language` — Chinese feeds
    get Chinese prompts (which makes NotebookLM answer in Chinese) so the
    generated email, images, and Instagram captions are all in the user's
    expected language without extra config.
    """
    raw = config.get("report_sections")
    if raw:
        return [(s["title"], s["prompt"]) for s in raw]
    if _is_chinese(config.get("whisper_language", "en")):
        return REPORT_SECTIONS_ZH
    return REPORT_SECTIONS


# ---------------------------------------------------------------------------
# nlm helpers
# ---------------------------------------------------------------------------

def _run_nlm(nlm_path: str, *args: str) -> subprocess.CompletedProcess:
    cmd = [nlm_path, *args]
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip() if result.stderr else ""
        raise RuntimeError(
            f"nlm command failed (exit {result.returncode}): {stderr or '(no stderr)'}"
        )
    return result


def create_briefing_doc(nlm_path: str, notebook_id: str, language: str = "en") -> None:
    """Ask NotebookLM to generate a Briefing Doc studio artifact."""
    try:
        _run_nlm(
            nlm_path,
            "report", "create", notebook_id,
            "--format", "Briefing Doc",
            "--language", language,
            "--confirm",
        )
        print("  Briefing Doc created in NotebookLM.")
    except RuntimeError as exc:
        # Non-fatal: the email can still be sent even if this fails
        print(f"  WARNING: Could not create Briefing Doc: {exc}")


# NotebookLM citation markers: [1], [1,2], [1-3], [4–6] (en-dash), [1，2] (FW
# comma). The outer group also consumes runs of citations separated only by
# commas/spaces — e.g. "[1], [2], [3]" — so we don't leave dangling commas
# behind after stripping each individual bracket.
_ONE_CITATION = r"\[\d+(?:\s*[,，\-–]\s*\d+)*\s*\]"
_CITATION_PATTERN = re.compile(
    rf"\s*{_ONE_CITATION}(?:\s*[,，]?\s*{_ONE_CITATION})*"
)


def _clean_answer(text: str) -> str:
    """Post-process a NotebookLM answer for human consumption.

    - Strips citation markers like [1], [1-3], [1,2,5], runs like [1], [2], [3].
    - Collapses spaces left over from the strips.
    - Strips trailing whitespace.
    """
    text = _CITATION_PATTERN.sub("", text)
    # Collapse runs of spaces inside a line (don't touch newlines)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def query_notebook(
    nlm_path: str,
    notebook_id: str,
    question: str,
    max_attempts: int = 3,
) -> str:
    """Send a question to the notebook and return the text response.

    Retries on transient `nlm` failures (occasional exit-1 with no stderr
    when the upstream NotebookLM service hiccups). Raises the last error
    only when every attempt fails.
    """
    import time
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = _run_nlm(nlm_path, "query", "notebook", notebook_id, question)
            raw = result.stdout.strip()
            # nlm returns a JSON envelope: {"value": {"answer": "...", ...}}
            try:
                data = json.loads(raw)
                answer = (
                    data.get("value", {}).get("answer")
                    or data.get("answer")
                    or raw
                )
            except (json.JSONDecodeError, AttributeError):
                answer = raw
            return _clean_answer(answer)
        except RuntimeError as exc:
            last_exc = exc
            if attempt < max_attempts:
                backoff = 5 * attempt
                print(f"    Retry {attempt}/{max_attempts - 1} — query failed ({exc}); waiting {backoff}s")
                time.sleep(backoff)

    assert last_exc is not None
    raise last_exc


def query_all_sections(nlm_path: str, notebook_id: str, config: dict | None = None) -> str:
    """Run each report section query independently and combine into one document.

    When `report_mode: stocks` is set in config, switches to per-stock sections:
    a single discovery query enumerates the stocks discussed, then each stock
    gets its own focused ## section. Otherwise behaves identically to before.
    """
    cfg = config or {}
    mode = cfg.get("report_mode")
    if mode in ("discover", "stocks"):  # "stocks" kept as a back-compat alias
        return query_per_item_sections(nlm_path, notebook_id, cfg)

    sections = _get_report_sections(cfg)
    parts = []
    for idx, (title, question) in enumerate(sections, start=1):
        print(f"  [{idx}/{len(sections)}] Querying: {title} …")
        try:
            answer = query_notebook(nlm_path, notebook_id, question)
        except RuntimeError as exc:
            print(f"    WARNING: query failed — {exc}")
            answer = "(This section query failed — please open the NotebookLM notebook directly.)"
        parts.append(f"## {title}\n\n{answer}")
        print(f"    → {len(answer):,} chars")
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def _format_date_range(folder_name: str) -> str:
    """Turn '20260218-20260225' into '2026/02/18 – 2026/02/25'."""
    try:
        start, end = folder_name.split("-")
        return f"{start[:4]}/{start[4:6]}/{start[6:]} – {end[:4]}/{end[4:6]}/{end[6:]}"
    except Exception:
        return folder_name


def build_email_body(folder_name: str, notebook_id: str, summary: str) -> str:
    """Return the plain-text version of the report (saved to disk)."""
    notebook_url = f"https://notebooklm.google.com/notebook/{notebook_id}"
    lines = [
        summary,
        "",
        "-" * 60,
        f"Full NotebookLM notebook: {notebook_url}",
        "",
        "(This email was generated automatically by podcast-summary)",
    ]
    return "\n".join(lines)


def build_html_email(folder_name: str, notebook_id: str, summary: str,
                     config: dict | None = None) -> str:
    """Render the summary markdown into a styled HTML email."""
    notebook_url  = f"https://notebooklm.google.com/notebook/{notebook_id}"
    date_range    = _format_date_range(folder_name)
    content_html  = md.markdown(summary, extensions=["extra", "tables"])
    report_title  = (config or {}).get("report_title", "Podcast Summary")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue",
                 Arial, sans-serif;
    background: #f4f6f9;
    margin: 0; padding: 0;
    color: #1a1a2e;
  }}
  .wrapper {{
    max-width: 720px;
    margin: 32px auto;
    background: #ffffff;
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 4px 24px rgba(0,0,0,0.08);
  }}
  .header {{
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 60%, #0f3460 100%);
    color: #ffffff;
    padding: 36px 40px 28px;
  }}
  .header h1 {{
    margin: 0 0 6px;
    font-size: 24px;
    font-weight: 700;
    letter-spacing: 0.5px;
  }}
  .header .date {{
    font-size: 14px;
    opacity: 0.7;
    margin: 0;
  }}
  .content {{
    padding: 36px 40px;
    line-height: 1.8;
    font-size: 15px;
  }}
  .content h2 {{
    font-size: 18px;
    font-weight: 700;
    color: #0f3460;
    border-left: 4px solid #e94560;
    padding-left: 12px;
    margin: 28px 0 14px;
  }}
  .content h3 {{
    font-size: 17px;
    font-weight: 700;
    color: #0f3460;
    margin: 20px 0 10px;
  }}
  .content ul {{
    padding-left: 20px;
    margin: 8px 0 16px;
  }}
  .content li {{
    margin-bottom: 8px;
  }}
  .content ul ul {{
    margin: 6px 0 6px;
  }}
  .content strong {{
    color: #0f3460;
  }}
  .content hr {{
    border: none;
    border-top: 1px solid #e8ecf0;
    margin: 28px 0;
  }}
  .content table {{
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0 24px;
    font-size: 14px;
  }}
  .content th {{
    background: #0f3460;
    color: #ffffff;
    padding: 10px 14px;
    text-align: left;
    font-weight: 600;
  }}
  .content td {{
    padding: 10px 14px;
    border-bottom: 1px solid #e8ecf0;
    vertical-align: top;
  }}
  .content tr:nth-child(even) td {{
    background: #f8f9fc;
  }}
  .cta {{
    margin: 32px 0 8px;
    text-align: center;
  }}
  .cta a {{
    display: inline-block;
    background: #e94560;
    color: #ffffff;
    text-decoration: none;
    padding: 13px 32px;
    border-radius: 8px;
    font-weight: 600;
    font-size: 15px;
    letter-spacing: 0.3px;
  }}
  .footer {{
    background: #f4f6f9;
    text-align: center;
    padding: 20px 40px;
    font-size: 12px;
    color: #999;
    border-top: 1px solid #e8ecf0;
  }}
</style>
</head>
<body>
  <div class="wrapper">
    <div class="header">
      <h1>🎙 {report_title}</h1>
      <p class="date">{date_range}</p>
    </div>
    <div class="content">
      {content_html}
      <div class="cta">
        <a href="{notebook_url}">Open NotebookLM Notebook →</a>
      </div>
    </div>
    <div class="footer">
      Generated automatically by podcast-summary
    </div>
  </div>
</body>
</html>"""


def send_email(config: dict, subject: str, plain_body: str, html_body: str) -> None:
    email_cfg = config.get("email", {})
    if not email_cfg:
        raise RuntimeError("No 'email' section found in config.yaml.")

    # Support email.to as either a string or a list of strings
    to_raw    = email_cfg["to"]
    to_list   = to_raw if isinstance(to_raw, list) else [to_raw]
    to_header = ", ".join(to_list)

    from_addr = email_cfg["from"]
    smtp_host = email_cfg.get("smtp_host", "smtp.gmail.com")
    smtp_port = int(email_cfg.get("smtp_port", 587))
    smtp_user = email_cfg.get("smtp_user", from_addr)

    # Prefer env var; fall back to config value (empty string → error)
    smtp_password = os.environ.get("EMAIL_SMTP_PASSWORD") or email_cfg.get("smtp_password", "")
    if not smtp_password:
        raise RuntimeError(
            "SMTP password not set.\n"
            "  Export EMAIL_SMTP_PASSWORD=<your-app-password>\n"
            "  or set smtp_password in config.yaml."
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = from_addr   # generic To: so recipients are hidden from each other
    msg["Bcc"]     = to_header
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,  "html",  "utf-8"))

    print(f"  Sending to {len(to_list)} recipient(s) via {smtp_host}:{smtp_port} …")
    with smtplib.SMTP(smtp_host, smtp_port) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.sendmail(from_addr, to_list, msg.as_string())
    print("  Email sent.")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def validate_report(summary: str) -> None:
    """Raise RuntimeError if the report summary looks malformed or too short."""
    stripped = summary.strip()
    if not stripped:
        raise RuntimeError("Report summary is empty — not sending email.")
    if len(stripped) < MIN_REPORT_CHARS:
        raise RuntimeError(
            f"Report summary is suspiciously short ({len(stripped)} chars < {MIN_REPORT_CHARS}). "
            "NotebookLM may not have processed the sources yet. Not sending email."
        )


def save_report(config: dict, folder_name: str, body: str) -> Path:
    """Save the report text to disk and return the file path."""
    report_dir = Path(config["source_folder"]) / "reports" / folder_name
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"weekly_report_{folder_name}.txt"
    report_path.write_text(body, encoding="utf-8")
    print(f"  Report saved to: {report_path}")
    return report_path


def run(
    config: dict,
    folder_name: str,
    notebook_id: str,
    send_email_flag: bool | None = None,
    generate_image_flag: bool | None = None,
    post_instagram_flag: bool | None = None,
) -> None:
    nlm_path        = config.get("nlm_path", "nlm")
    notebook_prefix = config.get("notebooklm_notebook_prefix", "Podcast Summary")
    date_range      = _format_date_range(folder_name)
    briefing_lang   = config.get("whisper_language", "en")

    # Step 1: Create a Briefing Doc artifact inside NotebookLM
    print("Generating Briefing Doc in NotebookLM …")
    create_briefing_doc(nlm_path, notebook_id, language=briefing_lang)

    # Step 2: Query the notebook section by section for a deep, detailed report
    print(f"\nQuerying notebook …")
    summary = query_all_sections(nlm_path, notebook_id, config=config)

    if not summary:
        summary = (
            "(NotebookLM returned no summary — please open the notebook directly.)\n"
            f"https://notebooklm.google.com/notebook/{notebook_id}"
        )

    # Step 3: Validate report before doing anything with it
    print("\nValidating report …")
    validate_report(summary)
    print(f"  ✓ Report looks good ({len(summary.strip())} chars).")

    subject    = f"{notebook_prefix} | {date_range}"
    plain_body = build_email_body(folder_name, notebook_id, summary)
    html_body  = build_html_email(folder_name, notebook_id, summary, config=config)

    # Step 4: Save report to disk
    print("\nSaving report …")
    report_path = save_report(config, folder_name, plain_body)

    # Step 5: Send the email — flag overrides config; config defaults to True
    email_cfg = config.get("email", {})
    do_email = send_email_flag if send_email_flag is not None else email_cfg.get("enabled", True)

    if do_email:
        print("\nSending email report …")
        send_email(config, subject, plain_body, html_body)
    else:
        print("\nEmail sending skipped (disabled in config or save-report-only mode).")

    # Step 6: Generate images — one per report section (optional, non-fatal)
    image_cfg = config.get("image_generation", {})
    do_image = generate_image_flag if generate_image_flag is not None else image_cfg.get("enabled", False)
    images: list[tuple[str | None, object]] = []

    if do_image:
        print("\nGenerating images …")
        try:
            import generate_image as _gen_image
            images = _gen_image.generate(config, summary, folder_name, report_path.parent)
        except Exception as exc:
            print(f"  WARNING: Image generation failed — {exc}")

    # Step 7: Post to Instagram (optional, non-fatal)
    ig_cfg = config.get("instagram", {})
    do_instagram = post_instagram_flag if post_instagram_flag is not None else ig_cfg.get("enabled", False)

    if do_instagram:
        image_urls = [url for url, _ in images if url]
        if not image_urls:
            print("\nInstagram posting skipped — no public image URLs available.")
        else:
            print(f"\nPosting {len(image_urls)} image(s) to Instagram …")
            try:
                import post_instagram as _post_ig
                _post_ig.post(config, image_urls, folder_name, summary=summary)
            except Exception as exc:
                print(f"  WARNING: Instagram posting failed — {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a NotebookLM report and email it."
    )
    parser.add_argument("--config", default="config.yaml",
                        help="Path to config YAML (default: config.yaml)")
    parser.add_argument("--notebook-id", required=True,
                        help="NotebookLM notebook ID to query and report on.")
    parser.add_argument("--folder", default=None,
                        help="Run folder name, e.g. 20260218-20260225.")
    args = parser.parse_args()

    config      = load_config(args.config)
    folder_name = args.folder or "unknown"

    run(config, folder_name, args.notebook_id)


if __name__ == "__main__":
    main()
