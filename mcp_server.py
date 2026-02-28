from __future__ import annotations
"""
mcp_server.py

MCP server for the podcast-summary pipeline.

Tools:
  Pipeline:  run_pipeline, get_run_status, resume_pipeline
  Reports:   list_reports, get_report
  Configs:   list_configs, get_config, set_config
  Cron:      list_cron_jobs, install_cron_job, remove_cron_job
  Debug:     get_logs

Start with:
  psum mcp          (via the CLI)
  python mcp_server.py
  venv14/bin/psum-mcp
"""

import json
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from mcp.server.fastmcp import FastMCP

PROJECT_ROOT      = Path(__file__).parent.resolve()
PSUM_CONFIG_DIR   = Path.home() / ".config" / "psum"
DEFAULT_CONFIG    = PSUM_CONFIG_DIR / "config.yaml"
CRON_MARKER_PREFIX = "# psum:"

# Ordered list of pipeline stages
PIPELINE_STAGES = ["fetch", "transcribe", "upload", "email", "cleanup"]

mcp = FastMCP("podcast-summary")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_config(config_path: Optional[str] = None) -> dict:
    path = Path(config_path).expanduser() if config_path else DEFAULT_CONFIG
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _status_file(cfg: dict, folder_name: str) -> Path:
    return Path(cfg["source_folder"]) / "runs" / folder_name / "status.json"


def _read_status(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_status(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _run_stages_background(
    cfg: dict,
    folder_name: str,
    stages: list[str],
    sf: Path,
    notebook_id: Optional[str] = None,
    send_email_flag: bool = True,
) -> None:
    """Background thread: run each stage sequentially, updating status.json between stages."""
    sys.path.insert(0, str(PROJECT_ROOT))
    import pipeline as pl  # noqa: PLC0415

    status = _read_status(sf)

    for stage in PIPELINE_STAGES:
        if stage not in stages:
            continue

        status["stages"][stage] = "running"
        status["status"] = "running"
        _write_status(sf, status)

        try:
            if stage == "fetch":
                ok = pl.run_fetch(cfg, folder_name)

            elif stage == "transcribe":
                result = pl.run_transcribe(cfg, folder_name)
                # run_transcribe returns True, "partial", or False
                ok = result is not False

            elif stage == "upload":
                result = pl.run_upload(cfg, folder_name)
                if result is False:
                    ok = False
                else:
                    notebook_id = result
                    status["notebook_id"] = notebook_id
                    ok = True

            elif stage == "email":
                if not notebook_id:
                    status["stages"][stage] = "skipped"
                    status["error"] = (
                        "Email skipped: no notebook_id available. "
                        "Re-run with a notebook_id or include the upload stage."
                    )
                    _write_status(sf, status)
                    continue
                ok = pl.run_email(cfg, folder_name, notebook_id,
                                  send_email_flag=send_email_flag)

            elif stage == "cleanup":
                pl.cleanup_old_data(cfg)
                ok = True

            else:
                ok = False

        except Exception as exc:  # noqa: BLE001
            status["stages"][stage] = "failed"
            status["status"] = "failed"
            status["error"] = f"Stage '{stage}' raised an exception: {exc}"
            _write_status(sf, status)
            return

        status["stages"][stage] = "completed" if ok else "failed"
        _write_status(sf, status)

        if not ok:
            status["status"] = "failed"
            status["error"] = f"Stage '{stage}' failed — see logs for details."
            _write_status(sf, status)
            return

    status["status"] = "completed"
    status["error"] = None
    _write_status(sf, status)


# ---------------------------------------------------------------------------
# Pipeline tools
# ---------------------------------------------------------------------------

@mcp.tool()
def run_pipeline(
    stages: Optional[list[str]] = None,
    folder: Optional[str] = None,
    notebook_id: Optional[str] = None,
    send_email: bool = True,
    config: Optional[str] = None,
) -> str:
    """Start the podcast-summary pipeline in the background (fire-and-forget).

    Returns immediately. Use get_run_status() to check progress.
    Use resume_pipeline() to retry after a failure.

    Args:
        stages: Stages to run — any of: fetch, transcribe, upload, email, cleanup.
                Omit to run all stages.
        folder: Date folder name, e.g. '20260218-20260225'. Auto-computed if omitted.
        notebook_id: Reuse an existing NotebookLM notebook ID (skips upload).
        send_email: If False, save the report to disk without sending email.
        config: Path or filename of a psum config YAML. Uses the default config if omitted.
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    import pipeline as pl  # noqa: PLC0415

    cfg         = _load_config(config)
    folder_name = folder or pl.default_folder_name(int(cfg.get("lookback_days", 7)))
    stages_to_run = list(stages) if stages is not None else list(PIPELINE_STAGES)

    # If a notebook_id is provided we don't need to upload
    if notebook_id and "upload" in stages_to_run:
        stages_to_run.remove("upload")

    sf = _status_file(cfg, folder_name)
    existing = _read_status(sf)
    if existing.get("status") == "running":
        return (
            f"A pipeline run for '{folder_name}' is already in progress.\n"
            f"Use get_run_status(folder='{folder_name}') to check progress."
        )

    initial = {
        "status":      "running",
        "config":      str(Path(config).expanduser() if config else DEFAULT_CONFIG),
        "folder":      folder_name,
        "started_at":  datetime.now(timezone.utc).isoformat(),
        "notebook_id": notebook_id,
        "error":       None,
        "stages": {
            s: ("pending" if s in stages_to_run else "skipped")
            for s in PIPELINE_STAGES
        },
    }
    _write_status(sf, initial)

    threading.Thread(
        target=_run_stages_background,
        args=(cfg, folder_name, stages_to_run, sf),
        kwargs={"notebook_id": notebook_id, "send_email_flag": send_email},
        daemon=True,
    ).start()

    return (
        f"Pipeline started in background.\n"
        f"  Folder : {folder_name}\n"
        f"  Stages : {', '.join(stages_to_run)}\n\n"
        f"Use get_run_status(folder='{folder_name}') to monitor progress."
    )


@mcp.tool()
def get_run_status(
    folder: Optional[str] = None,
    config: Optional[str] = None,
) -> str:
    """Get the current status of a pipeline run.

    Args:
        folder: Run folder, e.g. '20260218-20260225'. Defaults to the latest run.
        config: Path or filename of a psum config YAML. Uses the default config if omitted.
    """
    cfg      = _load_config(config)
    runs_dir = Path(cfg["source_folder"]) / "runs"

    if folder is None:
        if not runs_dir.exists():
            return "No runs found."
        folders = sorted([d.name for d in runs_dir.iterdir() if d.is_dir()], reverse=True)
        if not folders:
            return "No runs found."
        folder = folders[0]

    data = _read_status(_status_file(cfg, folder))
    if not data:
        return f"No status file found for folder: {folder}"

    icons = {"completed": "✓", "running": "⟳", "failed": "✗",
             "pending": "·", "skipped": "–"}

    lines = [
        f"Run     : {data.get('folder', folder)}",
        f"Status  : {data.get('status', '?')}",
        f"Config  : {data.get('config', '?')}",
        f"Started : {data.get('started_at', '?')}",
        f"Updated : {data.get('updated_at', '?')}",
    ]
    if data.get("notebook_id"):
        lines.append(f"Notebook: {data['notebook_id']}")
    if data.get("error"):
        lines.append(f"Error   : {data['error']}")
    lines.append("\nStages:")
    for stage, st in data.get("stages", {}).items():
        lines.append(f"  {icons.get(st, '?')}  {stage:<12} {st}")

    return "\n".join(lines)


@mcp.tool()
def resume_pipeline(
    folder: Optional[str] = None,
    config: Optional[str] = None,
    send_email: bool = True,
) -> str:
    """Resume a failed or incomplete pipeline run from the last failed stage.

    Reads the run's status.json, skips all completed stages, and retries
    from the point of failure. The notebook_id captured during upload is
    reused automatically so the email stage can proceed without re-uploading.

    Args:
        folder: Run folder to resume. Defaults to the latest failed run.
        config: Path or filename of a psum config YAML. Uses the default config if omitted.
        send_email: If False, save the report without sending email.
    """
    cfg      = _load_config(config)
    runs_dir = Path(cfg["source_folder"]) / "runs"

    if folder is None:
        if not runs_dir.exists():
            return "No runs found."
        failed = [
            d.name for d in runs_dir.iterdir()
            if d.is_dir() and _read_status(d / "status.json").get("status") == "failed"
        ]
        if not failed:
            return "No failed runs found."
        folder = sorted(failed, reverse=True)[0]

    sf   = _status_file(cfg, folder)
    data = _read_status(sf)
    if not data:
        return f"No status file found for folder: {folder}"
    if data.get("status") == "completed":
        return f"Run '{folder}' already completed — nothing to resume."
    if data.get("status") == "running":
        return f"Run '{folder}' is already in progress."

    # Stages that are not yet completed (failed or pending)
    stages_to_run = [
        s for s in PIPELINE_STAGES
        if data.get("stages", {}).get(s) not in ("completed", "skipped")
    ]
    if not stages_to_run:
        return "No stages to resume."

    notebook_id = data.get("notebook_id")

    # Reset those stages to pending
    for s in stages_to_run:
        data["stages"][s] = "pending"
    data["status"] = "running"
    data["error"]  = None
    _write_status(sf, data)

    threading.Thread(
        target=_run_stages_background,
        args=(cfg, folder, stages_to_run, sf),
        kwargs={"notebook_id": notebook_id, "send_email_flag": send_email},
        daemon=True,
    ).start()

    return (
        f"Resuming pipeline for '{folder}'.\n"
        f"  Stages      : {', '.join(stages_to_run)}\n"
        f"  Notebook ID : {notebook_id or '(none — upload will run)'}\n\n"
        f"Use get_run_status(folder='{folder}') to monitor progress."
    )


# ---------------------------------------------------------------------------
# Report tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_reports(config: Optional[str] = None) -> str:
    """List all saved weekly report folders.

    Args:
        config: Path or filename of a psum config YAML. Uses the default config if omitted.
    """
    cfg         = _load_config(config)
    reports_dir = Path(cfg["source_folder"]) / "reports"
    runs_dir    = Path(cfg["source_folder"]) / "runs"

    if not reports_dir.exists():
        return "No reports directory found."

    folders = sorted(
        [d.name for d in reports_dir.iterdir() if d.is_dir()],
        reverse=True,
    )
    if not folders:
        return "No reports found."

    lines = ["Available reports (newest first):"]
    for f in folders:
        has_report = (reports_dir / f / f"weekly_report_{f}.txt").exists()
        run_status = _read_status(runs_dir / f / "status.json").get("status", "")
        status_tag = f"  [{run_status}]" if run_status else ""
        report_tag = "✓ report" if has_report else "no report file"
        lines.append(f"  {f}  {report_tag}{status_tag}")

    return "\n".join(lines)


@mcp.tool()
def get_report(folder: Optional[str] = None, config: Optional[str] = None) -> str:
    """Get the content of a saved weekly report.

    Args:
        folder: Report folder name, e.g. '20260218-20260225'. Defaults to the latest.
        config: Path or filename of a psum config YAML. Uses the default config if omitted.
    """
    cfg         = _load_config(config)
    reports_dir = Path(cfg["source_folder"]) / "reports"

    if folder is None:
        if not reports_dir.exists():
            return "No reports directory found."
        folders = sorted(
            [d.name for d in reports_dir.iterdir() if d.is_dir()],
            reverse=True,
        )
        if not folders:
            return "No reports found."
        folder = folders[0]

    report_path = reports_dir / folder / f"weekly_report_{folder}.txt"
    if not report_path.exists():
        return f"Report not found: {report_path}"

    return report_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Config tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_configs() -> str:
    """List all psum config files available in ~/.config/psum/."""
    if not PSUM_CONFIG_DIR.exists():
        return "~/.config/psum/ does not exist."
    files = sorted(PSUM_CONFIG_DIR.glob("*.yaml"))
    if not files:
        return "No config files found."
    lines = ["Available configs:"]
    for f in files:
        tag = "  (default)" if f == DEFAULT_CONFIG else ""
        lines.append(f"  {f.name}{tag}  →  {f}")
    return "\n".join(lines)


@mcp.tool()
def get_config(config: Optional[str] = None) -> str:
    """Show a psum config file. SMTP password is redacted.

    Args:
        config: Config filename (e.g. 'stock-report.yaml') or full path.
                Defaults to the default config.
    """
    if config:
        path = Path(config).expanduser() if "/" in config else PSUM_CONFIG_DIR / config
    else:
        path = DEFAULT_CONFIG

    if not path.exists():
        return f"Config not found: {path}"

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "email" in data and "smtp_password" in data["email"]:
        data["email"]["smtp_password"] = "***"

    return f"# {path}\n\n{yaml.dump(data, allow_unicode=True, default_flow_style=False)}"


@mcp.tool()
def set_config(key: str, value: str, config: Optional[str] = None) -> str:
    """Set a key in a psum config file using dot notation.

    Args:
        key:    Dot-notation key, e.g. 'lookback_days' or 'email.to'.
        value:  New value. Strings, integers, floats, and booleans are auto-detected.
        config: Config filename or full path. Defaults to the default config.
    """
    if config:
        path = Path(config).expanduser() if "/" in config else PSUM_CONFIG_DIR / config
    else:
        path = DEFAULT_CONFIG

    if not path.exists():
        return f"Config not found: {path}"

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    # Auto-detect type
    parsed: object = value
    if value.lower() in ("true", "false"):
        parsed = value.lower() == "true"
    else:
        try:
            parsed = int(value)
        except ValueError:
            try:
                parsed = float(value)
            except ValueError:
                pass

    # Navigate / create nested keys
    parts = key.split(".")
    node  = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = parsed

    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )
    return f"Set {key} = {parsed!r} in {path.name}"


# ---------------------------------------------------------------------------
# Cron tools
# ---------------------------------------------------------------------------

def _get_crontab() -> list[str]:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return result.stdout.splitlines() if result.returncode == 0 else []


def _set_crontab(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    subprocess.run(["crontab", "-"], input=text, text=True, check=True)


@mcp.tool()
def list_cron_jobs() -> str:
    """List all psum cron jobs currently installed in crontab."""
    lines = _get_crontab()
    jobs  = [(line) for line in lines if CRON_MARKER_PREFIX in line]
    if not jobs:
        return "No psum cron jobs installed."

    parts = [f"{len(jobs)} psum cron job(s) installed:"]
    for line in jobs:
        idx  = line.index(CRON_MARKER_PREFIX)
        name = line[idx + len(CRON_MARKER_PREFIX):].strip()
        parts.append(f"\n  Name : {name}")
        parts.append(f"  Entry: {line.strip()}")
    return "\n".join(parts)


@mcp.tool()
def install_cron_job(
    name: str,
    schedule: str,
    config: Optional[str] = None,
) -> str:
    """Install a named psum cron job.

    Args:
        name:     Job name, e.g. 'stock-report'. Used to identify the job later.
        schedule: Cron schedule expression, e.g. '0 8 * * 0' (Sundays at 8 am).
        config:   Config filename or full path. Defaults to the default config.
    """
    if config:
        config_path = Path(config).expanduser() if "/" in config else PSUM_CONFIG_DIR / config
    else:
        config_path = DEFAULT_CONFIG

    if not config_path.exists():
        return f"Config not found: {config_path}"

    run_sh   = PROJECT_ROOT / "run.sh"
    marker   = f"{CRON_MARKER_PREFIX}{name}"
    new_line = (
        f"{schedule} {run_sh} --config {config_path} "
        f"> /dev/null 2>&1  {marker}"
    )

    lines = _get_crontab()
    idx   = next((i for i, l in enumerate(lines) if marker in l), None)
    if idx is not None:
        lines[idx] = new_line
        action = "updated"
    else:
        lines.append(new_line)
        action = "installed"

    _set_crontab(lines)
    return f"Cron job '{name}' {action}:\n  {new_line}"


@mcp.tool()
def remove_cron_job(name: str) -> str:
    """Remove a named psum cron job from crontab.

    Args:
        name: Job name to remove, e.g. 'stock-report'.
    """
    marker    = f"{CRON_MARKER_PREFIX}{name}"
    lines     = _get_crontab()
    new_lines = [l for l in lines if marker not in l]

    if len(new_lines) == len(lines):
        return f"No cron job named '{name}' found."

    _set_crontab(new_lines)
    return f"Cron job '{name}' removed."


# ---------------------------------------------------------------------------
# Debug tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_logs(lines: int = 100) -> str:
    """Get the last N lines of the pipeline log.

    Args:
        lines: Number of lines to return (default 100).
    """
    log_path = PROJECT_ROOT / "logs" / "pipeline.log"
    if not log_path.exists():
        return "No pipeline.log found."

    all_lines = log_path.read_text(encoding="utf-8").splitlines()
    tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
    return "\n".join(tail)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
