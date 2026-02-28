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
        "Takeaways & Action Items",
        (
            "List the most important takeaways and any concrete recommendations or action items "
            "mentioned across all episodes this week. Organise them by topic or priority."
        ),
    ),
]


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
    """Return report sections from config if defined, else use built-in defaults."""
    raw = config.get("report_sections")
    if raw:
        return [(s["title"], s["prompt"]) for s in raw]
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


def query_notebook(nlm_path: str, notebook_id: str, question: str) -> str:
    """Send a question to the notebook and return the text response."""
    result = _run_nlm(nlm_path, "query", "notebook", notebook_id, question)
    raw = result.stdout.strip()
    # nlm returns a JSON envelope: {"value": {"answer": "...", ...}}
    # Extract just the markdown answer text.
    try:
        data = json.loads(raw)
        answer = (
            data.get("value", {}).get("answer")
            or data.get("answer")
            or raw
        )
        return answer.strip()
    except (json.JSONDecodeError, AttributeError):
        return raw


def query_all_sections(nlm_path: str, notebook_id: str, config: dict | None = None) -> str:
    """Run each report section query independently and combine into one document."""
    sections = _get_report_sections(config or {})
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
    msg["To"]      = to_header
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,  "html",  "utf-8"))

    print(f"  Sending to {to_header} via {smtp_host}:{smtp_port} …")
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
    report_dir = Path(config["parent_folder"]) / "reports" / folder_name
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"weekly_report_{folder_name}.txt"
    report_path.write_text(body, encoding="utf-8")
    print(f"  Report saved to: {report_path}")
    return report_path


def run(config: dict, folder_name: str, notebook_id: str, send_email_flag: bool = True) -> None:
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
    save_report(config, folder_name, plain_body)

    # Step 5: Send the email (skipped when send_email_flag=False)
    if send_email_flag:
        print("\nSending email report …")
        send_email(config, subject, plain_body, html_body)
    else:
        print("\nEmail sending skipped (save-report-only mode).")


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
