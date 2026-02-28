from __future__ import annotations
"""
mcp_server.py

MCP server for the podcast-summary pipeline.
Exposes four tools: run_pipeline, get_report, list_reports, get_logs.

Start with:
  psum mcp          (via the CLI)
  python mcp_server.py
  venv14/bin/psum-mcp
"""

import subprocess
from pathlib import Path
from typing import Optional

import yaml
from mcp.server.fastmcp import FastMCP

PROJECT_ROOT = Path(__file__).parent.resolve()
DEFAULT_CONFIG = PROJECT_ROOT / "config.yaml"

mcp = FastMCP("podcast-summary")


def _load_config(config_path: Optional[str] = None) -> dict:
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ─── Tools ───────────────────────────────────────────────────────────────────

@mcp.tool()
def run_pipeline(
    stages: Optional[list[str]] = None,
    folder: Optional[str] = None,
    notebook_id: Optional[str] = None,
    send_email: bool = True,
    config: Optional[str] = None,
) -> str:
    """Run the podcast-summary pipeline.

    Args:
        stages: Stages to run — any of: fetch, transcribe, upload, email, cleanup.
                Omit to run all stages.
        folder: Date folder name, e.g. '20260218-20260225'. Auto-computed if omitted.
        notebook_id: Reuse an existing NotebookLM notebook ID (skips the upload stage).
        send_email: If False, generate and save the report to disk without sending email.
                    Defaults to True.
        config: Path to config.yaml. Uses the project default if omitted.
    """
    run_sh = PROJECT_ROOT / "run.sh"
    cmd = [str(run_sh)]

    if config:
        cmd += ["--config", config]
    if folder:
        cmd += ["--folder", folder]
    if notebook_id:
        cmd += ["--notebook-id", notebook_id]
    if not send_email:
        cmd.append("--save-report-only")

    if stages is not None:
        all_stages = {"fetch", "transcribe", "upload", "email", "cleanup"}
        for s in all_stages - set(stages):
            cmd.append(f"--skip-{s}")

    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT)
    )

    parts = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(f"STDERR:\n{result.stderr}")
    parts.append(f"\nExit code: {result.returncode}")
    return "\n".join(parts)


@mcp.tool()
def list_reports() -> str:
    """List all available weekly report folders."""
    cfg = _load_config()
    reports_dir = Path(cfg.get("parent_folder", "")) / "reports"
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
        has_report = (reports_dir / f / "weekly_report.txt").exists()
        status = "✓" if has_report else "no report file"
        lines.append(f"  {f}  [{status}]")
    return "\n".join(lines)


@mcp.tool()
def get_report(folder: Optional[str] = None) -> str:
    """Get the content of a weekly report.

    Args:
        folder: Report folder name, e.g. '20260218-20260225'. Defaults to the latest.
    """
    cfg = _load_config()
    reports_dir = Path(cfg.get("parent_folder", "")) / "reports"

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

    report_path = reports_dir / folder / "weekly_report.txt"
    if not report_path.exists():
        return f"Report not found: {report_path}"

    return report_path.read_text(encoding="utf-8")


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


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
