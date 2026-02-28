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


# ─── init ─────────────────────────────────────────────────────────────────────

@main.command()
@click.pass_context
def init(ctx):
    """Interactive first-time setup wizard."""
    config_path = ctx.obj["config"]
    cfg = _load_cfg(config_path)

    click.echo("\n=== Podcast Summary — Setup Wizard ===\n")

    # 0. Project root (where pipeline.py and venv/ live)
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
    project_root_input = click.prompt("Project root [required]", default=suggested_root)
    project_root_path = Path(project_root_input).expanduser().resolve()
    if not (project_root_path / "pipeline.py").exists():
        click.echo(f"  ! pipeline.py not found in {project_root_path} — fix this before running the pipeline")
    else:
        click.echo(f"  ✓ pipeline.py found at {project_root_path}")

    # 1. Parent folder
    default_folder = cfg.get("parent_folder", str(Path.home() / "psum-data"))
    parent_folder = click.prompt("Data folder path [required]", default=default_folder)

    # 2. nlm — installed automatically by postinstall; check auth status
    nlm_path_expanded = cfg.get("nlm_path", "")
    if nlm_path_expanded and Path(nlm_path_expanded).exists():
        click.echo(f"  ✓ nlm found at {nlm_path_expanded}")
        auth_ok = subprocess.run(
            [nlm_path_expanded, "login", "--check"],
            capture_output=True,
        ).returncode == 0
        if auth_ok:
            click.echo("  ✓ nlm already authenticated")
        elif click.confirm("  Log in to NotebookLM now? [optional — needed for upload stage]", default=True):
            subprocess.run([nlm_path_expanded, "login"], check=False)
    else:
        click.echo("  ! nlm not found — run: psum config set nlm_path /path/to/nlm")
        click.echo("    (or reinstall: npm install -g podcast-summary)")

    # 3. SMTP password → ~/.zprofile (optional)
    existing_password = os.environ.get("EMAIL_SMTP_PASSWORD", "")
    if existing_password:
        click.echo("\n  EMAIL_SMTP_PASSWORD is already set in environment.")
        change_pw = click.confirm("  Update it?", default=False)
        if change_pw:
            smtp_password = click.prompt(
                "Gmail App Password [optional, saved to ~/.zprofile]", hide_input=True
            )
            _write_zprofile_var("EMAIL_SMTP_PASSWORD", smtp_password)
            click.echo("  ✓ Saved to ~/.zprofile")
        else:
            smtp_password = existing_password
    else:
        set_pw = click.confirm(
            "\nSet Gmail App Password now? [optional — needed for the email stage]", default=False
        )
        if set_pw:
            smtp_password = click.prompt(
                "Gmail App Password (saved to ~/.zprofile)", hide_input=True
            )
            _write_zprofile_var("EMAIL_SMTP_PASSWORD", smtp_password)
            click.echo("  ✓ Saved to ~/.zprofile")
        else:
            smtp_password = ""
            click.echo("  Skipped — set EMAIL_SMTP_PASSWORD in ~/.zprofile before using the email stage.")

    # 4. Sender email
    default_from = cfg.get("email", {}).get("from", "")
    from_email = click.prompt("\nSender email (Gmail address) [required]", default=default_from)

    # 5. Recipient email(s)
    existing_to = cfg.get("email", {}).get("to", "")
    if isinstance(existing_to, list):
        default_to = ", ".join(existing_to)
    else:
        default_to = existing_to or ""
    to_raw = click.prompt(
        "Recipient email(s), comma-separated [required]", default=default_to
    )
    to_list = [e.strip() for e in to_raw.split(",") if e.strip()]
    to_value = to_list if len(to_list) > 1 else (to_list[0] if to_list else "")

    # 6. Test SMTP connection (optional)
    if smtp_password and click.confirm("\nTest SMTP connection now?", default=True):
        try:
            smtp_host = cfg.get("email", {}).get("smtp_host", "smtp.gmail.com")
            smtp_port = int(cfg.get("email", {}).get("smtp_port", 587))
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(from_email, smtp_password)
            click.echo("  ✓ SMTP connection successful!")
        except Exception as exc:
            click.echo(f"  ✗ SMTP test failed: {exc}")

    # 7. Retention settings
    click.echo("\n--- Retention Settings [optional, press Enter to keep defaults] ---")
    retention = cfg.get("retention", {})
    audio_months = click.prompt(
        "Keep audio files for (months, 0 = never delete)",
        default=retention.get("audio_months", 3),
        type=int,
    )
    transcripts_months = click.prompt(
        "Keep transcripts for (months, 0 = never delete)",
        default=retention.get("transcripts_months", 0),
        type=int,
    )
    reports_months = click.prompt(
        "Keep reports for (months, 0 = never delete)",
        default=retention.get("reports_months", 0),
        type=int,
    )

    # 8. Write config.yaml
    email_cfg = cfg.get("email", {})
    email_cfg.update({
        "to": to_value,
        "from": from_email,
        "smtp_host": email_cfg.get("smtp_host", "smtp.gmail.com"),
        "smtp_port": email_cfg.get("smtp_port", 587),
        "smtp_user": from_email,
        "smtp_password": "",
    })
    cfg.setdefault("feeds", [])
    cfg.setdefault("lookback_days", 7)
    cfg.setdefault("whisper_model", "medium")
    cfg.setdefault("whisper_language", "en")
    cfg.setdefault("notebooklm_notebook_prefix", "Podcast Summary")
    cfg.update({
        "project_root": str(project_root_path),
        "parent_folder": parent_folder,
        "email": email_cfg,
        "retention": {
            "audio_months": audio_months,
            "transcripts_months": transcripts_months,
            "reports_months": reports_months,
        },
    })
    _save_cfg(config_path, cfg)
    click.echo(f"\n✓ Config saved to {config_path}")

    # 9. Offer to install cron job
    if click.confirm("\nInstall weekly cron job now?", default=True):
        click.echo("Common schedules:")
        click.echo("  '0 8 * * 0'  — Sundays at 8 AM (default)")
        click.echo("  '0 9 * * 0'  — Sundays at 9 AM")
        click.echo("  '0 8 * * 1'  — Mondays at 8 AM")
        click.echo("  '0 8 * * *'  — Every day at 8 AM")
        schedule = click.prompt("Cron schedule", default="0 8 * * 0")
        _install_cron_job(schedule, config_path, DEFAULT_JOB_NAME)


# ─── run ──────────────────────────────────────────────────────────────────────

@main.command("run")
@click.option("--skip-fetch",      is_flag=True, help="Skip the fetch/download stage.")
@click.option("--skip-transcribe", is_flag=True, help="Skip the transcription stage.")
@click.option("--skip-upload",     is_flag=True, help="Skip the NotebookLM upload stage.")
@click.option("--skip-email",      is_flag=True, help="Skip report generation and email entirely.")
@click.option("--skip-cleanup",    is_flag=True, help="Skip the data cleanup stage.")
@click.option("--save-report-only", is_flag=True,
              help="Generate and save report to disk without sending email.")
@click.option("--folder",      default=None, help="Run folder, e.g. 20260218-20260225.")
@click.option("--notebook-id", default=None, help="Reuse an existing NotebookLM notebook ID.")
@click.pass_context
def run_cmd(ctx, skip_fetch, skip_transcribe, skip_upload, skip_email, skip_cleanup,
            save_report_only, folder, notebook_id):
    """Run the full pipeline (or specific stages)."""
    config_path = ctx.obj["config"]
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
    if skip_fetch:       cmd.append("--skip-fetch")
    if skip_transcribe:  cmd.append("--skip-transcribe")
    if skip_upload:      cmd.append("--skip-upload")
    if skip_email:       cmd.append("--skip-email")
    if skip_cleanup:     cmd.append("--skip-cleanup")
    if save_report_only: cmd.append("--save-report-only")
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
    cfg = _load_cfg(ctx.obj["config"])
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
    config_path = ctx.obj["config"]
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
    config_path = ctx.obj["config"]
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
@click.option(
    "--schedule",
    default="0 8 * * 0",
    show_default=True,
    help=(
        "Cron expression. Examples:\n"
        "  '0 8 * * 0'  — Sundays at 8 AM\n"
        "  '0 9 * * 0'  — Sundays at 9 AM\n"
        "  '0 8 * * 1'  — Mondays at 8 AM\n"
        "  '0 8 * * *'  — Every day at 8 AM"
    ),
)
@click.option(
    "--name",
    default=DEFAULT_JOB_NAME,
    show_default=True,
    help="Job name used to identify this cron entry. Use different names for different configs.",
)
@click.pass_context
def cron_install(ctx, schedule, name):
    """Install (or update) a named pipeline cron job.

    Each job is identified by --name and points to a config file.
    If multiple configs exist and --config is not specified,
    you will be prompted to select one.

    Example — install a second job for a different config:
      psum cron install --name tech-pods --schedule "0 9 * * 0"
      (then select the config interactively)
    """
    config_path = _pick_config(ctx.obj["config"])
    if not config_path.exists():
        click.echo(f"Config file not found: {config_path}")
        click.echo("Run 'psum init' to create one.")
        return
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
@click.pass_context
def config_show(ctx):
    """Display a configuration file (passwords redacted).

    If multiple configs exist and --config is not specified,
    you will be prompted to select one.
    """
    config_path = _pick_config(ctx.obj["config"])
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
@click.argument("key")
@click.argument("value")
@click.pass_context
def config_set(ctx, key, value):
    """Set a config value using dot notation (e.g. retention.audio_months 6)."""
    config_path = ctx.obj["config"]
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
    cfg = _load_cfg(ctx.obj["config"])
    nlm_path = cfg.get("nlm_path", "")
    if not nlm_path or not Path(nlm_path).exists():
        click.echo("Error: nlm not found. Set it with: psum config set nlm_path /path/to/nlm", err=True)
        sys.exit(1)
    auth_ok = subprocess.run([nlm_path, "login", "--check"], capture_output=True).returncode == 0
    if auth_ok:
        click.echo("Already authenticated. Use --force to re-login.")
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
