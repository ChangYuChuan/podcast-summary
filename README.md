# podcast-summary (`psum`)

A command-line tool that automatically fetches podcast episodes, transcribes them with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), uploads transcripts to [NotebookLM](https://notebooklm.google.com), and emails a weekly AI-generated report — all on a configurable cron schedule.

Multiple named jobs and configs are supported, so you can run completely different podcast sets with different prompts from a single installation.

---

## How it works

```
fetch → transcribe → upload → email → cleanup
```

| Stage | What happens |
|---|---|
| **fetch** | Parses RSS feeds, finds episodes within `lookback_days`, downloads audio |
| **transcribe** | Runs faster-whisper locally to produce per-episode `.txt` transcripts |
| **upload** | Creates a fresh NotebookLM notebook and uploads all transcripts |
| **email** | Queries the notebook section-by-section, renders a styled HTML report, sends via SMTP |
| **cleanup** | Deletes audio/transcript/report files older than the configured retention period |

---

## Requirements

- **Python 3.10+** (CLI and pipeline)
- **Node.js 18+** (for `npm install -g` only)
- A **Gmail account** with an [App Password](https://support.google.com/accounts/answer/185833) for sending email
- **[nlm](https://github.com/Jordy-Baby/notebooklm-mcp-cli)** — installed automatically

---

## Installation

```bash
# Install from npm (creates ~/.config/psum/venv automatically)
npm install -g podcast-summary

# Add psum to your PATH
echo 'export PATH="$HOME/.config/psum/venv/bin:$PATH"' >> ~/.zprofile
source ~/.zprofile

# Set up the pipeline venv (needed for transcription — heavier dependencies)
cd "$(npm root -g)/podcast-summary"
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### Why two venvs?

| Venv | Location | Purpose |
|---|---|---|
| CLI venv | `~/.config/psum/venv/` | Lightweight — `psum` CLI + `nlm`. Created by postinstall. |
| Pipeline venv | `<npm_package_dir>/venv/` | Heavy — `faster-whisper`, feedparser, etc. Created manually. |

---

## Quick start

```bash
# Interactive setup wizard
psum init

# Run the full pipeline
psum run

# Run a specific config
psum --config ~/.config/psum/stock-report.yaml run
```

---

## Configuration

Configs live in `~/.config/psum/`. Copy the example to get started:

```bash
cp config.yaml.example ~/.config/psum/config.yaml
```

```yaml
# ~/.config/psum/config.yaml

project_root: /path/to/podcast-summary   # where pipeline.py lives
source_folder: /path/to/psum-data        # where audio/transcripts/reports are stored

feeds:
  - name: My Podcast
    url: https://example.com/feed.xml
  - name: Another Show
    url: https://example.com/other-feed.xml

lookback_days: 7
whisper_model: medium        # tiny / base / small / medium / large / large-v3
whisper_language: en

notebooklm_notebook_prefix: Podcast Summary
nlm_path: /Users/you/.config/psum/venv/bin/nlm

# Title shown in the email header (default: "Podcast Summary")
report_title: My Weekly Digest

email:
  to:
    - you@example.com
    - colleague@example.com
  from: sender@gmail.com
  smtp_host: smtp.gmail.com
  smtp_port: 587
  smtp_user: sender@gmail.com
  smtp_password: ""   # leave blank — use EMAIL_SMTP_PASSWORD env var

# Data retention (0 = keep forever)
retention:
  audio_months: 3
  transcripts_months: 0
  reports_months: 0

# Optional: override the default report sections sent to NotebookLM.
# If omitted, a generic 3-section English template is used.
# report_sections:
#   - title: "Episode Summaries"
#     prompt: "Summarise each episode with key arguments and conclusions."
#   - title: "Key Themes"
#     prompt: "What were the major themes across all episodes this week?"
```

Set your SMTP password as an environment variable (add to `~/.zprofile`):

```bash
export EMAIL_SMTP_PASSWORD="your-gmail-app-password"
```

---

## CLI reference

### Setup

```bash
psum init                          # Interactive wizard: create/edit config + optional cron
psum nlm-login                     # Authenticate with NotebookLM (one-time)
```

### Running the pipeline

```bash
psum run                           # Full pipeline
psum run --skip-fetch              # Skip download (use existing audio)
psum run --skip-fetch --skip-transcribe          # Upload + email only
psum run --skip-fetch --skip-transcribe \
         --notebook-id <id>        # Email only (reuse existing notebook)
psum run --skip-report             # Fetch + transcribe only (no NLM)
psum run --save-report-only        # Generate report but don't send email
psum run --folder 20260218-20260225  # Target a specific week
```

### Config management

```bash
psum config list                   # List all configs in ~/.config/psum/
psum config show                   # Display a config (passwords redacted)
psum config create                 # Create new config or update existing
psum config set retention.audio_months 6   # Set a value (dot notation)
```

### Feeds & recipients

```bash
psum podcast list                  # List configured feeds
psum podcast add "My Show" https://example.com/feed.xml
psum podcast remove "My Show"

psum receiver list                 # List email recipients
psum receiver add colleague@example.com
psum receiver remove colleague@example.com
```

### Cron jobs

```bash
psum cron install                  # Install job with default schedule (Sundays 8am)
psum cron install --schedule "0 9 * * 0" --name stock-report
psum cron status                   # Show all installed psum jobs
psum cron remove --name stock-report
```

---

## Multiple configs & named jobs

Each config is an independent YAML file in `~/.config/psum/`. Each cron job is tagged with a name so they coexist safely in crontab.

```bash
# Create a second config for a different podcast set
psum config create   # → creates ~/.config/psum/tech-pods.yaml

# Install a separate cron job for it
psum --config ~/.config/psum/tech-pods.yaml \
     cron install --name tech-pods --schedule "0 9 * * 1"

# See both jobs
psum cron status
# Status: 2 job(s) installed
#   Name:  default
#   Entry: 0 8 * * 0 /…/run.sh --config /…/config.yaml  # psum:default
#
#   Name:  tech-pods
#   Entry: 0 9 * * 1 /…/run.sh --config /…/tech-pods.yaml  # psum:tech-pods
```

When multiple configs exist, CLI commands that need a config will prompt you to pick one interactively (unless `--config` is specified).

---

## MCP server (Claude integration)

`psum` ships an [MCP](https://modelcontextprotocol.io) server so Claude can control the pipeline directly.

```bash
psum mcp    # start the server (stdio transport)
```

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "podcast-summary": {
      "command": "/Users/you/.config/psum/venv/bin/psum-mcp"
    }
  }
}
```

### Available MCP tools

| Tool | Description |
|---|---|
| `run_pipeline` | Start the pipeline in the background (fire-and-forget) |
| `get_run_status` | Check stage-by-stage progress of a run |
| `resume_pipeline` | Retry a failed run from the last failed stage |
| `list_reports` | List saved weekly reports |
| `get_report` | Read the content of a report |
| `list_configs` | List all available config files |
| `get_config` | Display a config (passwords redacted) |
| `set_config` | Update a config value (dot notation) |
| `list_cron_jobs` | Show all installed psum cron jobs |
| `install_cron_job` | Install or update a named cron job |
| `remove_cron_job` | Remove a named cron job |
| `get_logs` | Tail the pipeline log |

The `run_pipeline` tool is fire-and-forget: it returns immediately and writes a `status.json` per run. Use `get_run_status` to monitor progress, and `resume_pipeline` to retry if a stage fails.

---

## Data layout

```
{source_folder}/
  audio/
    {podcast_name}/
      {podcast_name}_20260221.mp3
  transcripts/
    {podcast_name}/
      {podcast_name}_20260221.txt
  reports/
    20260221-20260228/
      weekly_report_20260221-20260228.txt
  runs/
    20260221-20260228/
      status.json          ← pipeline run status (MCP)
```

---

## Uninstall

```bash
psum cron remove --name default   # remove cron jobs first
rm -rf ~/.config/psum/            # remove CLI venv and configs
rm -rf ./venv/                    # remove pipeline venv
```
