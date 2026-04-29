from __future__ import annotations
"""
cli.py

psum — Podcast Summary CLI.

Usage:
  psum --help
  psum init                          # Interactive setup wizard
  psum run [options]                 # Run the pipeline
  psum podcast list/add/remove       # Manage podcast feeds
  psum receiver list/add/remove      # Manage email recipients
  psum cron install/remove/status    # Manage cron jobs
  psum config show/set               # View/update config values
  psum mcp                           # Start the MCP server
"""

import os
import re
import smtplib
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

# Project root = directory containing this file
PROJECT_ROOT = Path(__file__).parent.resolve()
DEFAULT_CONFIG = Path.home() / ".config" / "psum" / "config.yaml"

CRON_MARKER_PREFIX = "# psum:"
DEFAULT_JOB_NAME   = "default"
PSUM_CONFIG_DIR    = Path.home() / ".config" / "psum"


# ─── Internal helpers ────────────────────────────────────────────────────────

def _load_cfg(config_path: Path) -> dict:
    from config_manager import load_config
    if not config_path.exists():
        return {}
    return load_config(config_path)


def _save_cfg(config_path: Path, data: dict) -> None:
    from config_manager import save_config
    save_config(config_path, data)


def _get_crontab() -> list[str]:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def _set_crontab(lines: list[str]) -> None:
    content = "\n".join(lines)
    if content and not content.endswith("\n"):
        content += "\n"
    subprocess.run(["crontab", "-"], input=content, text=True, check=True)


def _cron_marker(name: str) -> str:
    return f"# psum:{name}"


def _find_cron_idx(lines: list[str], name: str) -> Optional[int]:
    marker = _cron_marker(name)
    for i, line in enumerate(lines):
        if marker in line:
            return i
    return None


def _find_all_crons(lines: list[str]) -> list[tuple[int, str]]:
    """Return (line_idx, job_name) for every psum cron entry."""
    results = []
    for i, line in enumerate(lines):
        if CRON_MARKER_PREFIX in line:
            idx = line.index(CRON_MARKER_PREFIX)
            name = line[idx + len(CRON_MARKER_PREFIX):].strip()
            results.append((i, name))
    return results


def _write_zprofile_var(var_name: str, value: str) -> None:
    """Write or update an export line in ~/.zprofile."""
    zprofile = Path.home() / ".zprofile"
    new_line = f'export {var_name}="{value}"'
    pattern = re.compile(rf"^export\s+{re.escape(var_name)}\s*=")

    if zprofile.exists():
        lines = zprofile.read_text(encoding="utf-8").splitlines(keepends=True)
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = new_line + "\n"
                zprofile.write_text("".join(lines), encoding="utf-8")
                return
        content = "".join(lines)
    else:
        content = ""

    if content and not content.endswith("\n"):
        content += "\n"
    content += new_line + "\n"
    zprofile.write_text(content, encoding="utf-8")


def _list_config_files() -> list[Path]:
    """Return all .yaml files in the psum config directory, sorted by name."""
    if not PSUM_CONFIG_DIR.exists():
        return []
    return sorted(PSUM_CONFIG_DIR.glob("*.yaml"))


def _pick_config(current: Path) -> Path:
    """If the user is using the default config and multiple configs exist,
    prompt them to select one interactively."""
    if current != DEFAULT_CONFIG:
        return current  # user explicitly passed --config, respect it
    configs = _list_config_files()
    if len(configs) <= 1:
        return current
    click.echo("Available configs:\n")
    for i, c in enumerate(configs, 1):
        marker = "  ← default" if c == DEFAULT_CONFIG else ""
        click.echo(f"  {i}. {c.name}{marker}")
    click.echo()
    choice = click.prompt(
        "Select config",
        type=click.IntRange(1, len(configs)),
        default=1,
    )
    return configs[choice - 1]


def _resolve_config(ctx_config: Path, name: str | None = None) -> Path:
    """Resolve a config path from an optional name argument.

    Priority:
      1. name arg → match by stem in PSUM_CONFIG_DIR (e.g. "tech" → tech.yaml)
      2. --config flag (explicit path) → use as-is
      3. Multiple configs exist → interactive picker
    """
    if name:
        stemmed = PSUM_CONFIG_DIR / f"{name}.yaml"
        if stemmed.exists():
            return stemmed
        exact = PSUM_CONFIG_DIR / name
        if exact.exists():
            return exact
        click.echo(f"Config '{name}' not found in {PSUM_CONFIG_DIR}", err=True)
        configs = _list_config_files()
        if configs:
            click.echo("Available configs: " + "  ".join(c.stem for c in configs), err=True)
        sys.exit(1)
    return _pick_config(ctx_config)


def _install_cron_job(schedule: str, config_path: Path, name: str) -> None:
    run_sh = PROJECT_ROOT / "run.sh"
    lines  = _get_crontab()
    idx    = _find_cron_idx(lines, name)
    new_line = (
        f"{schedule} {run_sh} --config {config_path} > /dev/null 2>&1"
        f"  {_cron_marker(name)}"
    )
    if idx is not None:
        lines[idx] = new_line
    else:
        lines.append(new_line)
    _set_crontab(lines)
    click.echo(f"Cron job installed: {new_line}")


# ─── Main group ──────────────────────────────────────────────────────────────

@click.group()
@click.option(
    "--config",
    default=str(DEFAULT_CONFIG),
    show_default=True,
    help="Path to config.yaml",
)
@click.pass_context
def main(ctx, config):
    """Podcast Summary — manage pipeline, feeds, recipients, and scheduling."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = Path(config)


# ─── Shared config wizard ─────────────────────────────────────────────────────

WIZARD_SECTIONS = [
    ("paths",     "Project & data paths"),
    ("feeds",     "Podcast feeds"),
    ("nlm",       "NotebookLM"),
    ("email",     "Email & SMTP"),
    ("retention", "Retention settings"),
    ("schedule",  "Schedule & lookback"),
    ("image",     "Cover image (OpenAI)"),
    ("instagram", "Instagram posting"),
    ("prompts",   "Report sections"),
]


def _prompt_or_skip(label: str, current: str, **kwargs) -> str:
    """Show current value as a numbered selection; return it unchanged or prompt for new."""
    if current:
        click.echo(f"\n  {label}")
        click.echo(f"    1. Keep current: {current}")
        click.echo(f"    2. Change")
        choice = click.prompt("  Selection", type=click.IntRange(1, 2), default=1)
        if choice == 1:
            return current
    return click.prompt(f"  New {label.lower()}", default=current or "", **kwargs)


def _section_summary(cfg: dict) -> dict[str, str]:
    """Return a one-line summary string for each wizard section."""
    feeds = cfg.get("feeds", [])
    ret   = cfg.get("retention", {})
    secs  = cfg.get("report_sections", [])
    to    = cfg.get("email", {}).get("to", "")
    to_str = ", ".join(to) if isinstance(to, list) else to
    return {
        "paths":     cfg.get("project_root", "") or "(not set)",
        "feeds":     f"{len(feeds)} feed(s)" if feeds else "(none)",
        "nlm":       cfg.get("nlm_path", "") or "(not set)",
        "email": (
            f"{'on' if cfg.get('email',{}).get('enabled', True) else 'off'} · "
            f"{cfg.get('email',{}).get('from','')} → {to_str}"
            if to_str else "(not set)"
        ),
        "retention": (
            f"audio {ret.get('audio_months','?')}mo · "
            f"transcripts {ret.get('transcripts_months','?')}mo · "
            f"reports {ret.get('reports_months','?')}mo"
        ) if ret else "(defaults)",
        "schedule":  (
            f"every {cfg.get('lookback_days', 7)}d · {cfg.get('schedule', '(not set)')}"
        ),
        "image": (
            f"enabled · {cfg.get('image_generation',{}).get('style','infographic')} · "
            f"max {cfg.get('image_generation',{}).get('max_images', 10)} images"
            if cfg.get("image_generation", {}).get("enabled")
            else "disabled"
        ),
        "instagram": (
            f"enabled · user_id={cfg.get('instagram',{}).get('user_id','?')}"
            if cfg.get("instagram", {}).get("enabled")
            else "disabled"
        ),
        "prompts":   f"{len(secs)} custom section(s)" if secs else "built-in defaults",
    }


def _pick_wizard_sections(cfg: dict) -> set[str]:
    """Show a section menu with current-value summaries; return chosen section keys."""
    summaries = _section_summary(cfg)
    click.echo("Which sections would you like to edit?\n")
    click.echo("  0. All")
    for i, (key, label) in enumerate(WIZARD_SECTIONS, 1):
        click.echo(f"  {i}. {label:<26} [{summaries[key]}]")
    click.echo()
    raw = click.prompt("Enter number(s), comma-separated (or 0 for all)", default="0")
    if raw.strip() == "0":
        return {key for key, _ in WIZARD_SECTIONS}
    chosen = set()
    for part in raw.split(","):
        try:
            idx = int(part.strip()) - 1
            if 0 <= idx < len(WIZARD_SECTIONS):
                chosen.add(WIZARD_SECTIONS[idx][0])
        except ValueError:
            pass
    return chosen or {key for key, _ in WIZARD_SECTIONS}


def _run_config_wizard(config_path: Path, sections: Optional[set[str]] = None) -> None:
    """Walk through config settings interactively and save the result.

    If `sections` is None, all sections are run (used for new configs).
    Otherwise only the specified section keys are prompted.
    """
    cfg = _load_cfg(config_path)
    active = sections if sections is not None else {key for key, _ in WIZARD_SECTIONS}

    # ── paths ──────────────────────────────────────────────────────────────────
    if "paths" in active:
        click.echo("\n--- Project & Data Paths ---")
        cwd = Path.cwd()
        saved_root = cfg.get("project_root", "")
        if (cwd / "pipeline.py").exists():
            suggested_root = str(cwd)
        elif saved_root and (Path(saved_root) / "pipeline.py").exists():
            suggested_root = saved_root
        elif (PROJECT_ROOT / "pipeline.py").exists():
            suggested_root = str(PROJECT_ROOT)
        else:
            suggested_root = saved_root or ""
        project_root_input = _prompt_or_skip("Project root", suggested_root)
        project_root_path = Path(project_root_input).expanduser().resolve()
        if not (project_root_path / "pipeline.py").exists():
            click.echo(f"  ! pipeline.py not found in {project_root_path}")
        else:
            click.echo(f"  ✓ pipeline.py found at {project_root_path}")

        default_folder = cfg.get("source_folder", str(Path.home() / "psum-data"))
        source_folder = _prompt_or_skip("Data folder path", default_folder)

        cfg["project_root"] = str(project_root_path)
        cfg["source_folder"] = source_folder

    # ── feeds ──────────────────────────────────────────────────────────────────
    if "feeds" in active:
        click.echo("\n--- Podcast Feeds ---")
        existing_feeds = cfg.get("feeds", [])
        if existing_feeds:
            click.echo(f"  Current feeds ({len(existing_feeds)}):")
            for i, f in enumerate(existing_feeds, 1):
                click.echo(f"    {i}. {f['name']}  {f['url']}")
            feeds = list(existing_feeds) if click.confirm("  Keep existing feeds?", default=True) else []
        else:
            click.echo("  No feeds configured yet.")
            feeds = []

        click.echo("  Add feeds one by one. Leave name blank to finish.\n")
        while True:
            name = click.prompt(f"  Feed {len(feeds) + 1} name", default="").strip()
            if not name:
                break
            url = click.prompt(f"  Feed {len(feeds) + 1} URL").strip()
            entry: dict = {"name": name, "url": url}
            if "youtube.com" in url or "youtu.be" in url:
                entry["type"] = "youtube"
                click.echo("    ↳ YouTube channel detected — will use transcript API with yt-dlp fallback")
            feeds.append(entry)
            click.echo()

        if feeds:
            click.echo(f"  ✓ {len(feeds)} feed(s) configured.")
        else:
            click.echo("  No feeds — add later with: psum podcast add <name> <url>")
        cfg["feeds"] = feeds

    # ── nlm ────────────────────────────────────────────────────────────────────
    if "nlm" in active:
        import shutil
        click.echo("\n--- NotebookLM ---")
        nlm_path_expanded = cfg.get("nlm_path", "")

        # Auto-detect: same venv as psum, or PATH
        if not nlm_path_expanded or not Path(nlm_path_expanded).exists():
            venv_nlm = Path(sys.executable).parent / "nlm"
            path_nlm = shutil.which("nlm") or ""
            auto = str(venv_nlm) if venv_nlm.exists() else path_nlm
            if auto:
                click.echo(f"  ✓ nlm found at {auto}")
                cfg["nlm_path"] = auto
                nlm_path_expanded = auto
            else:
                click.echo("  ! nlm not found (should have been installed with psum).")
                click.echo("    Try: pip install notebooklm-mcp-cli")

        if nlm_path_expanded and Path(nlm_path_expanded).exists():
            auth_ok = subprocess.run(
                [nlm_path_expanded, "login", "--check"], capture_output=True,
            ).returncode == 0
            if auth_ok:
                click.echo("  ✓ nlm already authenticated")
            elif click.confirm("  Log in to NotebookLM now?", default=True):
                subprocess.run([nlm_path_expanded, "login"], check=False)

    # ── email ──────────────────────────────────────────────────────────────────
    if "email" in active:
        click.echo("\n--- Email & SMTP ---")
        email_cfg = dict(cfg.get("email", {}))

        # SMTP password
        existing_password = os.environ.get("EMAIL_SMTP_PASSWORD", "")
        if existing_password:
            click.echo("  EMAIL_SMTP_PASSWORD is already set in environment.")
            if click.confirm("  Update it?", default=False):
                pw = click.prompt("Gmail App Password (saved to ~/.zprofile)", hide_input=True)
                _write_zprofile_var("EMAIL_SMTP_PASSWORD", pw)
                click.echo("  ✓ Saved to ~/.zprofile")
                smtp_password = pw
            else:
                smtp_password = existing_password
        else:
            if click.confirm("Set Gmail App Password now? [needed for email stage]", default=False):
                smtp_password = click.prompt(
                    "Gmail App Password (saved to ~/.zprofile)", hide_input=True
                )
                _write_zprofile_var("EMAIL_SMTP_PASSWORD", smtp_password)
                click.echo("  ✓ Saved to ~/.zprofile")
            else:
                smtp_password = ""
                click.echo("  Skipped — set EMAIL_SMTP_PASSWORD in ~/.zprofile before using the email stage.")

        # Sender
        from_email = _prompt_or_skip("Sender email (Gmail address)", email_cfg.get("from", ""))

        # Recipients — show existing, then enter one per line
        existing_to = email_cfg.get("to", "")
        existing_list = existing_to if isinstance(existing_to, list) else ([existing_to] if existing_to else [])
        if existing_list:
            click.echo(f"  Current recipients ({len(existing_list)}):")
            for addr in existing_list:
                click.echo(f"    • {addr}")
            recipients = list(existing_list) if click.confirm("  Keep existing recipients?", default=True) else []
        else:
            recipients = []

        click.echo("  Add recipients one by one. Leave blank to finish.\n")
        while True:
            addr = click.prompt(f"  Recipient {len(recipients) + 1} email", default="").strip()
            if not addr:
                break
            if addr in recipients:
                click.echo(f"    (already in list, skipping)")
            else:
                recipients.append(addr)
        click.echo(f"  ✓ {len(recipients)} recipient(s): {', '.join(recipients)}" if recipients else "  No recipients configured.")

        # SMTP test
        if smtp_password and click.confirm("\nTest SMTP connection now?", default=True):
            try:
                smtp_host = email_cfg.get("smtp_host", "smtp.gmail.com")
                smtp_port = int(email_cfg.get("smtp_port", 587))
                with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
                    smtp.ehlo(); smtp.starttls(); smtp.login(from_email, smtp_password)
                click.echo("  ✓ SMTP connection successful!")
            except Exception as exc:
                click.echo(f"  ✗ SMTP test failed: {exc}")

        send_enabled = click.confirm(
            "\n  Send report via email when the pipeline runs?",
            default=email_cfg.get("enabled", True),
        )

        to_value = recipients if len(recipients) > 1 else (recipients[0] if recipients else "")
        email_cfg.update({
            "enabled": send_enabled,
            "to": to_value,
            "from": from_email,
            "smtp_host": email_cfg.get("smtp_host", "smtp.gmail.com"),
            "smtp_port": email_cfg.get("smtp_port", 587),
            "smtp_user": from_email,
            "smtp_password": "",
        })
        cfg["email"] = email_cfg

    # ── retention ──────────────────────────────────────────────────────────────
    if "retention" in active:
        click.echo("\n--- Retention Settings (0 = keep forever) ---")
        retention = cfg.get("retention", {})
        cfg["retention"] = {
            "audio_months": click.prompt(
                "Keep audio files for (months)", default=retention.get("audio_months", 3), type=int,
            ),
            "transcripts_months": click.prompt(
                "Keep transcripts for (months)", default=retention.get("transcripts_months", 0), type=int,
            ),
            "reports_months": click.prompt(
                "Keep reports for (months)", default=retention.get("reports_months", 0), type=int,
            ),
        }

    # ── schedule ───────────────────────────────────────────────────────────────
    if "schedule" in active:
        click.echo("\n--- Schedule & Lookback ---")
        click.echo(
            "  lookback_days controls how many days of episodes each run covers.\n"
            "  Match the cron schedule so runs don't overlap or leave gaps.\n"
        )
        click.echo("  Common patterns:")
        click.echo("    lookback_days: 1  +  schedule: '0 8 * * *'    → daily at 8 AM")
        click.echo("    lookback_days: 3  +  schedule: '0 8 */3 * *'  → every 3 days")
        click.echo("    lookback_days: 7  +  schedule: '0 8 * * 0'    → weekly (Sunday)")
        click.echo()
        lookback_days = click.prompt(
            "  Days of episodes per run (lookback_days)",
            default=cfg.get("lookback_days", 7),
            type=int,
        )
        schedule_val = click.prompt(
            "  Cron schedule",
            default=cfg.get("schedule", "0 8 * * 0"),
        )
        cfg["lookback_days"] = lookback_days
        cfg["schedule"] = schedule_val

    # ── image generation ───────────────────────────────────────────────────────
    if "image" in active:
        click.echo("\n--- Cover Image Generation (OpenAI) ---")
        image_cfg = dict(cfg.get("image_generation", {}))
        currently_enabled = image_cfg.get("enabled", False)
        enable_image = click.confirm(
            "  Enable cover image generation?", default=currently_enabled
        )
        image_cfg["enabled"] = enable_image

        if enable_image:
            existing_key = os.environ.get("OPENAI_API_KEY", "")
            if existing_key:
                click.echo("  OPENAI_API_KEY is already set in environment.")
                if click.confirm("  Update it?", default=False):
                    new_key = click.prompt("  OpenAI API key", hide_input=True)
                    _write_zprofile_var("OPENAI_API_KEY", new_key)
                    click.echo("  ✓ Saved to ~/.zprofile")
            else:
                click.echo("  OPENAI_API_KEY is not set.")
                if click.confirm("  Set it now?", default=True):
                    new_key = click.prompt("  OpenAI API key", hide_input=True)
                    _write_zprofile_var("OPENAI_API_KEY", new_key)
                    click.echo("  ✓ Saved to ~/.zprofile")
                else:
                    click.echo("  Skipped — export OPENAI_API_KEY=<key> before running.")

            image_cfg["model"] = click.prompt(
                "  Model", default=image_cfg.get("model", "gpt-image-2")
            )
            image_cfg["size"] = click.prompt(
                "  Size", default=image_cfg.get("size", "1024x1024")
            )
            image_cfg["quality"] = click.prompt(
                "  Quality (standard / hd)", default=image_cfg.get("quality", "standard")
            )
            click.echo("  Style options:")
            click.echo("    infographic  — readable summary card with highlights (default)")
            click.echo("    illustration — abstract art representing the themes")
            image_cfg["style"] = click.prompt(
                "  Style", default=image_cfg.get("style", "infographic"),
                type=click.Choice(["infographic", "illustration"]),
            )
            image_cfg["max_images"] = click.prompt(
                "  Max images per report (one per section; Instagram carousel limit is 10)",
                default=image_cfg.get("max_images", 10),
                type=click.IntRange(1, 10),
            )
            existing_style_base = image_cfg.get("style_base", "")
            click.echo(
                "  style_base: shared visual description applied to every image for consistency.\n"
                "  Leave blank to use the built-in default for the chosen style."
            )
            style_base_input = click.prompt(
                "  Style base (optional)", default=existing_style_base
            ).strip()
            if style_base_input:
                image_cfg["style_base"] = style_base_input
            else:
                image_cfg.pop("style_base", None)

        cfg["image_generation"] = image_cfg

    # ── instagram ──────────────────────────────────────────────────────────────
    if "instagram" in active:
        click.echo("\n--- Instagram Posting ---")
        click.echo(
            "  Requires: Instagram Business/Creator account, Facebook App with\n"
            "  instagram_content_publish permission, and a long-lived access token.\n"
            "  Docs: https://developers.facebook.com/docs/instagram-platform/instagram-graph-api\n"
        )
        ig_cfg = dict(cfg.get("instagram", {}))
        currently_enabled = ig_cfg.get("enabled", False)
        enable_ig = click.confirm(
            "  Enable Instagram posting?", default=currently_enabled
        )
        ig_cfg["enabled"] = enable_ig

        if enable_ig:
            existing_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
            if existing_token:
                click.echo("  INSTAGRAM_ACCESS_TOKEN is already set in environment.")
                if click.confirm("  Update it?", default=False):
                    new_token = click.prompt("  Instagram long-lived access token", hide_input=True)
                    _write_zprofile_var("INSTAGRAM_ACCESS_TOKEN", new_token)
                    click.echo("  ✓ Saved to ~/.zprofile")
            else:
                click.echo("  INSTAGRAM_ACCESS_TOKEN is not set.")
                if click.confirm("  Set it now?", default=True):
                    new_token = click.prompt("  Instagram long-lived access token", hide_input=True)
                    _write_zprofile_var("INSTAGRAM_ACCESS_TOKEN", new_token)
                    click.echo("  ✓ Saved to ~/.zprofile")
                else:
                    click.echo("  Skipped — export INSTAGRAM_ACCESS_TOKEN=<token> before running.")

            click.echo(
                "  Instagram user ID is optional — auto-discovered from the token.\n"
                "  Leave blank to let the pipeline look it up automatically."
            )
            user_id_input = click.prompt(
                "  Instagram user ID (optional)", default=ig_cfg.get("user_id", "")
            ).strip()
            if user_id_input:
                ig_cfg["user_id"] = user_id_input
            else:
                ig_cfg.pop("user_id", None)
            ig_cfg["api_version"] = click.prompt(
                "  Graph API version", default=ig_cfg.get("api_version", "v21.0")
            )

        cfg["instagram"] = ig_cfg

    # ── prompts ────────────────────────────────────────────────────────────────
    if "prompts" in active:
        click.echo("\n--- Report Sections (prompts sent to NotebookLM) ---")
        existing_sections = cfg.get("report_sections", [])
        if existing_sections:
            click.echo(f"  Current sections ({len(existing_sections)}):")
            for i, s in enumerate(existing_sections, 1):
                click.echo(f"    {i}. {s['title']}")
        else:
            click.echo("  Currently using built-in defaults.")
        click.echo()

        if existing_sections:
            click.echo("  1. Keep current sections")
            click.echo("  2. Configure new sections")
            click.echo("  3. Clear (use built-in defaults)")
            choice = click.prompt("  Choice", type=click.IntRange(1, 3), default=1)
        else:
            choice = 2 if click.confirm("  Configure custom report sections?", default=False) else 1

        if choice == 1:
            click.echo("  Keeping existing sections.")
        elif choice == 2:
            new_sections: list[dict] = []
            click.echo("  Enter sections one by one. Leave title blank to finish.\n")
            while True:
                title = click.prompt(f"  Section {len(new_sections) + 1} title", default="").strip()
                if not title:
                    break
                prompt_text = click.prompt(f"  Section {len(new_sections) + 1} prompt").strip()
                new_sections.append({"title": title, "prompt": prompt_text})
                click.echo()
            if new_sections:
                cfg["report_sections"] = new_sections
                click.echo(f"  ✓ {len(new_sections)} section(s) configured.")
            else:
                click.echo("  No sections entered — keeping existing sections.")
        else:
            cfg.pop("report_sections", None)
            click.echo("  Cleared — will use built-in defaults.")

    # ── defaults & save ────────────────────────────────────────────────────────
    cfg.setdefault("feeds", [])
    cfg.setdefault("lookback_days", 7)
    cfg.setdefault("schedule", "0 8 * * 0")
    cfg.setdefault("whisper_model", "medium")
    cfg.setdefault("whisper_language", "en")
    cfg.setdefault("notebooklm_notebook_prefix", "Podcast Summary")
    _save_cfg(config_path, cfg)
    click.echo(f"\n✓ Config saved to {config_path}")


def _select_existing_config() -> Path:
    """Prompt the user to pick from existing configs. Returns the chosen path."""
    existing = _list_config_files()
    if len(existing) == 1:
        click.echo(f"  ✓ Using: {existing[0].name}")
        return existing[0]
    for i, c in enumerate(existing, 1):
        marker = "  ← default" if c == DEFAULT_CONFIG else ""
        click.echo(f"  {i}. {c.name}{marker}")
    click.echo()
    idx = click.prompt(
        "Select config",
        type=click.IntRange(1, len(existing)),
        default=1,
    )
    return existing[idx - 1]


# ─── init ─────────────────────────────────────────────────────────────────────

@main.command()
@click.pass_context
def init(ctx):
    """Full setup wizard: create/edit a config then optionally install a cron job."""
    click.echo("\n=== Podcast Summary — Setup Wizard ===\n")

    existing = _list_config_files()
    if existing and ctx.obj["config"] == DEFAULT_CONFIG:
        click.echo("What would you like to do?\n")
        click.echo("  1. Edit an existing config")
        click.echo("  2. Create a new config")
        click.echo()
        action = click.prompt("Choice", type=click.Choice(["1", "2"]), default="1")
        if action == "1":
            click.echo()
            config_path = _select_existing_config()
            click.echo(f"\n  Working on: {config_path}\n")
            existing_cfg = _load_cfg(config_path)
            sections = _pick_wizard_sections(existing_cfg)
            _run_config_wizard(config_path, sections=sections)
        else:
            config_name = click.prompt("New config name (saved as ~/.config/psum/<name>.yaml)")
            config_path = PSUM_CONFIG_DIR / f"{config_name}.yaml"
            click.echo(f"\n  Working on: {config_path}\n")
            _run_config_wizard(config_path)
    else:
        config_path = ctx.obj["config"]
        _run_config_wizard(config_path)

    # Offer to install cron job
    if click.confirm("\nInstall a cron job for this config?", default=True):
        fresh_cfg = _load_cfg(config_path)
        suggested = fresh_cfg.get("schedule", "0 8 * * 0")
        lookback  = fresh_cfg.get("lookback_days", 7)
        click.echo(f"  Schedule from config: {suggested}  [lookback_days: {lookback}]")
        if not click.confirm("  Use this schedule?", default=True):
            click.echo("  Common patterns:")
            click.echo("    '0 8 * * *'   — daily at 8 AM")
            click.echo("    '0 8 */3 * *' — every 3 days at 8 AM")
            click.echo("    '0 8 * * 0'   — weekly, Sundays at 8 AM")
            suggested = click.prompt("  Cron schedule", default=suggested)
        job_name = click.prompt("Job name", default=config_path.stem)
        _install_cron_job(suggested, config_path, job_name)


# ─── run ──────────────────────────────────────────────────────────────────────

@main.command("run")
@click.argument("config_name", required=False, default=None,
                metavar="[CONFIG]")
@click.option("--skip-fetch",      is_flag=True, help="Skip the fetch/download stage.")
@click.option("--skip-transcribe", is_flag=True, help="Skip the transcription stage.")
@click.option("--skip-report",     is_flag=True, help="Skip NotebookLM upload AND email report.")
@click.option("--skip-cleanup",    is_flag=True, help="Skip the data cleanup stage.")
@click.option("--save-report-only", is_flag=True,
              help="Generate and save report to disk without sending email.")
@click.option("--skip-image",    is_flag=True, help="Skip cover image generation.")
@click.option("--skip-instagram", is_flag=True, help="Skip Instagram posting.")
@click.option("--folder",      default=None, help="Run folder, e.g. 20260218-20260225.")
@click.option("--notebook-id", default=None,
              help="Reuse an existing NotebookLM notebook ID (skips upload, runs email).")
@click.pass_context
def run_cmd(ctx, config_name, skip_fetch, skip_transcribe, skip_report, skip_cleanup,
            save_report_only, skip_image, skip_instagram, folder, notebook_id):
    """Run the full pipeline (or specific stages).

    CONFIG is the config name to use (e.g. 'tech' for tech.yaml).
    If omitted and multiple configs exist, you will be prompted to pick one.

    Stages: fetch → transcribe → report (upload + email) → cleanup

    Examples:
      psum run                                      # full run (pick config if needed)
      psum run tech                                 # run with tech.yaml
      psum run tech --skip-fetch                    # transcribe onwards
      psum run tech --skip-fetch --skip-transcribe  # upload + email only
      psum run tech --notebook-id <id>              # email only (reuse notebook)
      psum run tech --skip-report                   # fetch + transcribe only
    """
    config_path = _resolve_config(ctx.obj["config"], config_name)
    cfg = _load_cfg(config_path)
    project_root = Path(cfg["project_root"]) if cfg.get("project_root") else PROJECT_ROOT
    python_bin = project_root / "venv" / "bin" / "python3"
    pipeline_py = project_root / "pipeline.py"

    if not python_bin.exists():
        click.echo(f"Error: Pipeline Python not found at {python_bin}", err=True)
        if not cfg.get("project_root"):
            click.echo(
                "project_root is not set in your config. Run 'psum init' to set it.",
                err=True,
            )
        else:
            click.echo(
                f"Set up the pipeline venv inside {project_root}:\n"
                "  python3 -m venv venv && venv/bin/pip install -r requirements.txt",
                err=True,
            )
        sys.exit(1)

    cmd = [str(python_bin), str(pipeline_py), "--config", str(config_path)]
    if skip_fetch:      cmd.append("--skip-fetch")
    if skip_transcribe: cmd.append("--skip-transcribe")
    if skip_report:
        cmd.append("--skip-upload")
        cmd.append("--skip-email")
    if skip_cleanup:     cmd.append("--skip-cleanup")
    if save_report_only: cmd.append("--save-report-only")
    if skip_image:       cmd.append("--skip-image")
    if skip_instagram:   cmd.append("--skip-instagram")
    if folder:           cmd += ["--folder", folder]
    if notebook_id:      cmd += ["--notebook-id", notebook_id]

    result = subprocess.run(cmd, cwd=str(project_root))
    sys.exit(result.returncode)


# ─── podcast ──────────────────────────────────────────────────────────────────

@main.group()
def podcast():
    """Manage podcast feeds."""
    pass


@podcast.command("list")
@click.pass_context
def podcast_list(ctx):
    """Print all configured podcast feeds."""
    cfg = _load_cfg(ctx.obj["config"])
    feeds = cfg.get("feeds", [])
    if not feeds:
        click.echo("No feeds configured.")
        return
    for i, feed in enumerate(feeds, 1):
        click.echo(f"  {i}. {feed['name']}")
        click.echo(f"     {feed['url']}")


@podcast.command("add")
@click.argument("name")
@click.argument("url")
@click.pass_context
def podcast_add(ctx, name, url):
    """Add a podcast feed."""
    config_path = ctx.obj["config"]
    cfg = _load_cfg(config_path)
    feeds = cfg.setdefault("feeds", [])
    for feed in feeds:
        if feed["name"] == name:
            click.echo(f"Feed '{name}' already exists.")
            return
    feeds.append({"name": name, "url": url})
    _save_cfg(config_path, cfg)
    click.echo(f"Added: {name}")


@podcast.command("remove")
@click.argument("name")
@click.pass_context
def podcast_remove(ctx, name):
    """Remove a podcast feed by name."""
    config_path = ctx.obj["config"]
    cfg = _load_cfg(config_path)
    feeds = cfg.get("feeds", [])
    new_feeds = [f for f in feeds if f["name"] != name]
    if len(new_feeds) == len(feeds):
        click.echo(f"Feed '{name}' not found.")
        return
    cfg["feeds"] = new_feeds
    _save_cfg(config_path, cfg)
    click.echo(f"Removed: {name}")


# ─── receiver ─────────────────────────────────────────────────────────────────

@main.group()
def receiver():
    """Manage email recipients."""
    pass


@receiver.command("list")
@click.pass_context
def receiver_list(ctx):
    """Print all configured email recipients."""
    cfg = _load_cfg(_pick_config(ctx.obj["config"]))
    to = cfg.get("email", {}).get("to", "")
    if not to:
        click.echo("No recipients configured.")
        return
    recipients = to if isinstance(to, list) else [to]
    for i, email in enumerate(recipients, 1):
        click.echo(f"  {i}. {email}")


@receiver.command("add")
@click.argument("email_addr")
@click.pass_context
def receiver_add(ctx, email_addr):
    """Add an email recipient."""
    config_path = _pick_config(ctx.obj["config"])
    cfg = _load_cfg(config_path)
    email_cfg = cfg.setdefault("email", {})
    to = email_cfg.get("to", "")
    recipients = to if isinstance(to, list) else ([to] if to else [])
    if email_addr in recipients:
        click.echo(f"'{email_addr}' is already in the recipient list.")
        return
    recipients.append(email_addr)
    email_cfg["to"] = recipients if len(recipients) > 1 else recipients[0]
    _save_cfg(config_path, cfg)
    click.echo(f"Added: {email_addr}")


@receiver.command("remove")
@click.argument("email_addr")
@click.pass_context
def receiver_remove(ctx, email_addr):
    """Remove an email recipient."""
    config_path = _pick_config(ctx.obj["config"])
    cfg = _load_cfg(config_path)
    email_cfg = cfg.setdefault("email", {})
    to = email_cfg.get("to", "")
    recipients = to if isinstance(to, list) else ([to] if to else [])
    new_recipients = [r for r in recipients if r != email_addr]
    if len(new_recipients) == len(recipients):
        click.echo(f"'{email_addr}' not found in recipient list.")
        return
    email_cfg["to"] = (
        new_recipients if len(new_recipients) > 1 else
        (new_recipients[0] if new_recipients else "")
    )
    _save_cfg(config_path, cfg)
    click.echo(f"Removed: {email_addr}")


# ─── cron ─────────────────────────────────────────────────────────────────────

@main.group()
def cron():
    """Manage pipeline cron jobs."""
    pass


@cron.command("install")
@click.argument("config_name", required=False, default=None,
                metavar="[CONFIG]")
@click.option(
    "--schedule",
    default=None,
    help=(
        "Cron expression. If omitted, reads `schedule` from config.yaml.\n"
        "Examples:\n"
        "  '0 8 * * 0'   — Sundays at 8 AM\n"
        "  '0 8 */3 * *' — Every 3 days at 8 AM\n"
        "  '0 8 * * *'   — Every day at 8 AM"
    ),
)
@click.option(
    "--name",
    default=DEFAULT_JOB_NAME,
    show_default=True,
    help="Job name used to identify this cron entry. Use different names for different configs.",
)
@click.pass_context
def cron_install(ctx, config_name, schedule, name):
    """Install (or update) a named pipeline cron job.

    CONFIG is the config name (e.g. 'tech' for tech.yaml).
    The schedule is read from config.schedule unless overridden with --schedule.

    Examples:
      psum cron install tech
      psum cron install tech --name tech-pods
    """
    config_path = _resolve_config(ctx.obj["config"], config_name)
    if not config_path.exists():
        click.echo(f"Config file not found: {config_path}")
        click.echo("Run 'psum init' to create one.")
        return

    if not schedule:
        cfg = _load_cfg(config_path)
        schedule = cfg.get("schedule")
        if schedule:
            click.echo(f"  Schedule (from config): {schedule}  "
                       f"[lookback_days: {cfg.get('lookback_days', 7)}]")
        else:
            click.echo("  No schedule set in config.")
            click.echo("  Common patterns:")
            click.echo("    '0 8 * * *'   — daily at 8 AM")
            click.echo("    '0 8 */3 * *' — every 3 days at 8 AM")
            click.echo("    '0 8 * * 0'   — weekly, Sundays at 8 AM")
            schedule = click.prompt("  Cron schedule", default="0 8 * * 0")

    lines = _get_crontab()
    idx = _find_cron_idx(lines, name)
    if idx is not None:
        click.echo(f"Existing entry: {lines[idx]}")
        if not click.confirm("Replace it?"):
            click.echo("Aborted.")
            return
    _install_cron_job(schedule, config_path, name)


@cron.command("remove")
@click.option(
    "--name",
    default=DEFAULT_JOB_NAME,
    show_default=True,
    help="Job name to remove.",
)
def cron_remove(name):
    """Remove a named pipeline cron job."""
    lines = _get_crontab()
    idx = _find_cron_idx(lines, name)
    if idx is None:
        click.echo(f"No cron job found with name '{name}'.")
        return
    removed = lines.pop(idx)
    _set_crontab(lines)
    click.echo(f"Removed: {removed}")


@cron.command("status")
def cron_status():
    """Show all installed psum cron jobs."""
    lines = _get_crontab()
    all_jobs = _find_all_crons(lines)
    if not all_jobs:
        click.echo("Status: no psum cron jobs installed")
        return
    click.echo(f"Status: {len(all_jobs)} job(s) installed\n")
    for idx, name in all_jobs:
        click.echo(f"  Name:  {name}")
        click.echo(f"  Entry: {lines[idx]}")
        click.echo()


# ─── config ───────────────────────────────────────────────────────────────────

@main.group("config")
def config_cmd():
    """View and update configuration values."""
    pass


@config_cmd.command("create")
def config_create():
    """Interactively create a new config or update an existing one."""
    existing = _list_config_files()
    if existing:
        click.echo("What would you like to do?\n")
        click.echo("  1. Update an existing config")
        click.echo("  2. Create a new config")
        click.echo()
        action = click.prompt("Choice", type=click.Choice(["1", "2"]), default="1")
        if action == "1":
            click.echo()
            config_path = _select_existing_config()
            click.echo(f"\n  Working on: {config_path}\n")
            existing_cfg = _load_cfg(config_path)
            sections = _pick_wizard_sections(existing_cfg)
            _run_config_wizard(config_path, sections=sections)
        else:
            config_name = click.prompt("New config name (saved as ~/.config/psum/<name>.yaml)")
            config_path = PSUM_CONFIG_DIR / f"{config_name}.yaml"
            click.echo(f"\n  + Creating: {config_path}\n")
            _run_config_wizard(config_path)
    else:
        config_name = click.prompt("Config name (saved as ~/.config/psum/<name>.yaml)")
        config_path = PSUM_CONFIG_DIR / f"{config_name}.yaml"
        click.echo(f"\n  + Creating: {config_path}\n")
        _run_config_wizard(config_path)


@config_cmd.command("list")
def config_list():
    """List all available config files in ~/.config/psum/."""
    configs = _list_config_files()
    if not configs:
        click.echo(f"No config files found in {PSUM_CONFIG_DIR}")
        click.echo("Run 'psum init' to create one.")
        return
    click.echo(f"Configs in {PSUM_CONFIG_DIR}:\n")
    for c in configs:
        marker = "  ← default" if c == DEFAULT_CONFIG else ""
        click.echo(f"  {c.name}{marker}")


@config_cmd.command("show")
@click.argument("config_name", required=False, default=None, metavar="[CONFIG]")
@click.pass_context
def config_show(ctx, config_name):
    """Display a configuration file (passwords redacted).

    \b
    Examples:
      psum config show tech      # show tech.yaml
      psum config show           # picker if multiple configs
    """
    config_path = _resolve_config(ctx.obj["config"], config_name)
    if not config_path.exists():
        click.echo(f"Config file not found: {config_path}")
        click.echo("Run 'psum init' to create it.")
        return
    from config_manager import load_config
    import yaml
    click.echo(f"# {config_path}\n")
    cfg = load_config(config_path)
    # Redact smtp_password
    if cfg.get("email", {}).get("smtp_password"):
        cfg["email"]["smtp_password"] = "***"
    click.echo(yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False))


@config_cmd.command("set")
@click.argument("args", nargs=-1, required=True)
@click.pass_context
def config_set(ctx, args):
    """Set a config value using dot notation.

    \b
    With a config name (explicit):
      psum config set tech retention.audio_months 6
      psum config set tech email.enabled false

    \b
    Without a config name (picker if multiple configs):
      psum config set retention.audio_months 6
    """
    if len(args) == 3:
        config_name, key, value = args
        config_path = _resolve_config(ctx.obj["config"], config_name)
    elif len(args) == 2:
        key, value = args
        config_path = _resolve_config(ctx.obj["config"])
    else:
        raise click.UsageError("Usage: psum config set [CONFIG] KEY VALUE")
    cfg = _load_cfg(config_path)
    parts = key.split(".")
    obj = cfg
    for part in parts[:-1]:
        obj = obj.setdefault(part, {})
    leaf = parts[-1]
    if value.lower() in ("true", "false"):
        obj[leaf] = value.lower() == "true"
    else:
        try:
            obj[leaf] = int(value)
        except ValueError:
            try:
                obj[leaf] = float(value)
            except ValueError:
                obj[leaf] = value
    _save_cfg(config_path, cfg)
    click.echo(f"Set {key} = {obj[leaf]}")


# ─── nlm-login ────────────────────────────────────────────────────────────────

@main.command("nlm-login")
@click.pass_context
def nlm_login_cmd(ctx):
    """Log in to NotebookLM (browser OAuth). Run once, or again if auth expires."""
    import shutil
    cfg = _load_cfg(ctx.obj["config"]) if ctx.obj["config"].exists() else {}
    nlm_path = cfg.get("nlm_path", "")

    # Auto-detect: same venv as psum, or PATH
    if not nlm_path or not Path(nlm_path).exists():
        venv_nlm = Path(sys.executable).parent / "nlm"
        nlm_path = str(venv_nlm) if venv_nlm.exists() else (shutil.which("nlm") or "")

    if not nlm_path or not Path(nlm_path).exists():
        click.echo("Error: nlm binary not found.", err=True)
        click.echo("  Reinstall psum: pip install -e .", err=True)
        click.echo("  Or install manually: pip install notebooklm-mcp-cli", err=True)
        sys.exit(1)

    auth_ok = subprocess.run([nlm_path, "login", "--check"], capture_output=True).returncode == 0
    if auth_ok:
        click.echo("Already authenticated.")
        if not click.confirm("Re-login anyway?", default=False):
            return
    result = subprocess.run([nlm_path, "login"], check=False)
    sys.exit(result.returncode)


# ─── mcp ──────────────────────────────────────────────────────────────────────

@main.command("mcp")
def mcp_cmd():
    """Start the MCP server (stdio transport)."""
    import mcp_server
    mcp_server.main()


if __name__ == "__main__":
    main()
