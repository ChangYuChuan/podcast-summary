# podcast-summary (`psum`)

A command-line tool that automatically fetches podcast episodes **and YouTube channel videos**, transcribes them with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (or YouTube captions when available), uploads everything to [NotebookLM](https://notebooklm.google.com), and produces a styled AI-generated report — emailed to subscribers, optionally with **AI-generated cover images** posted to Instagram as a carousel — all on a configurable cron schedule.

Each config file is a fully independent pipeline. Reports default to **English or Traditional Chinese** based on `whisper_language`, and a built-in "Stocks Mentioned" section extracts every ticker the hosts discuss along with their stance.

---

## How it works

```
fetch → transcribe → upload → report → cleanup
```

| Stage | What happens |
|---|---|
| **fetch** | Parses RSS feeds + YouTube channels (videos & livestreams), finds episodes within `lookback_days`. RSS audio is downloaded directly; YouTube videos try the caption API first and fall back to `yt-dlp` audio download |
| **transcribe** | Runs faster-whisper locally on any audio file that didn't already get a caption-API transcript |
| **upload** | Creates a fresh NotebookLM notebook and uploads all transcripts |
| **report** | Queries the notebook section-by-section (defaults to Chinese sections when `whisper_language: zh`), renders a styled HTML email, optionally generates one cover image per section via OpenAI and posts them as an Instagram carousel |
| **cleanup** | Deletes audio/transcript/report files older than the configured retention period |

---

## Requirements

- **Python 3.11+**
- A **Gmail account** with an [App Password](https://support.google.com/accounts/answer/185833) for SMTP (only if you want emails sent — `email.enabled: false` skips this)
- A **Google account** for NotebookLM (browser OAuth via `psum nlm-login`)
- _(Optional)_ **OpenAI API key** for cover image generation
- _(Optional)_ **Instagram Business/Creator account** + long-lived access token for Instagram posting

`ffmpeg` is bundled via the `imageio-ffmpeg` Python wheel — no system install required for YouTube audio post-processing.

---

## Installation

```bash
# 1. Clone the repo
git clone <repo-url> && cd podcast-summary

# 2. Create the CLI venv and install psum (also installs nlm automatically)
python3.11 -m venv ~/.config/psum/venv
~/.config/psum/venv/bin/pip install -e .

# 3. Add to PATH (use ~/.zshenv so subprocesses + cron jobs see it too)
echo 'export PATH="$HOME/.config/psum/venv/bin:$PATH"' >> ~/.zshenv
source ~/.zshenv

# 4. Create the pipeline venv (heavier dependencies: whisper, feedparser, etc.)
python3.11 -m venv venv
venv/bin/pip install -r requirements.txt
```

### Why two venvs?

| Venv | Location | Contents |
|---|---|---|
| CLI venv | `~/.config/psum/venv/` | `psum`, `nlm` — lightweight, always available |
| Pipeline venv | `<repo>/venv/` | `faster-whisper`, `feedparser`, `openai`, etc. |

The CLI venv stays lean so `psum` is fast to start. The pipeline venv carries the heavy ML dependencies and is only invoked when a run starts.

---

## Quick start

```bash
# Authenticate with NotebookLM (one-time browser OAuth)
psum nlm-login

# Interactive setup wizard: config + cron
psum init

# Run the pipeline
psum run
psum run tech           # run a specific config by name
```

---

## Configuration

Configs live in `~/.config/psum/` as plain YAML files. `psum init` creates them interactively. You can also copy the example:

```bash
cp config.yaml.example ~/.config/psum/config.yaml
```

### Core options

```yaml
project_root: /path/to/podcast-summary   # where pipeline.py lives
source_folder: /path/to/psum-data        # audio / transcripts / reports

feeds:
  # RSS podcast feed
  - name: My Podcast
    url: https://example.com/feed.xml
  # YouTube channel — captions fetched via API, falls back to yt-dlp + Whisper
  - name: Lex Fridman
    url: https://www.youtube.com/@lexfridman
    type: youtube
    language: en    # optional — defaults to whisper_language

# How many days of episodes each run covers.
# Pair with `schedule` so runs don't overlap or leave gaps.
lookback_days: 7
schedule: "0 8 * * 0"     # cron used by `psum cron install`

whisper_model: medium      # tiny / base / small / medium / large / large-v3
whisper_language: en

notebooklm_notebook_prefix: Podcast Summary
nlm_path: /Users/you/.config/psum/venv/bin/nlm   # set automatically by wizard

report_title: My Weekly Digest
```

### Email

```yaml
email:
  enabled: true
  to:
    - you@example.com
    - colleague@example.com
  from: sender@gmail.com
  smtp_host: smtp.gmail.com
  smtp_port: 587
  smtp_user: sender@gmail.com
  smtp_password: ""    # leave blank — use EMAIL_SMTP_PASSWORD env var
```

```bash
export EMAIL_SMTP_PASSWORD="your-gmail-app-password"   # add to ~/.zshenv
```

### Schedule

```yaml
# Common patterns — match lookback_days to the interval so runs don't overlap:
lookback_days: 1
schedule: "0 8 * * *"      # daily at 8 AM

lookback_days: 3
schedule: "0 8 */3 * *"    # every 3 days at 8 AM

lookback_days: 7
schedule: "0 8 * * 0"      # weekly, Sundays at 8 AM
```

### Data retention

```yaml
retention:
  audio_months: 3        # delete audio older than 3 months
  transcripts_months: 0  # 0 = keep forever
  reports_months: 0
```

### Cover image generation (OpenAI)

Generates one image per report section (infographic cards or illustrations), suitable for an Instagram carousel. Prompts are written in Traditional Chinese automatically when `whisper_language: zh`.

```yaml
image_generation:
  enabled: true
  model: gpt-image-2          # or dall-e-3
  size: 1024x1024
  quality: medium             # gpt-image-2: low | medium | high | auto
                              # dall-e-3:    standard | hd
  style: infographic          # infographic (summary card) | illustration (abstract art)
  max_images: 10              # cap — one image per report section
  language: ""                # optional: override (defaults to whisper_language)
  style_base: ""              # optional: shared visual style for consistency across all cards
  # prompt_template: "..."    # optional override; placeholders: {report_title} {date_range}
  #                           #   {section_title} {card_label} {highlights} {themes}
```

```bash
export OPENAI_API_KEY="sk-..."   # add to ~/.zshenv
```

**Note:** `gpt-image-2` (and the `gpt-image-*` family) only return base64 image data. To bridge the gap to Instagram's Graph API — which requires a publicly-fetchable URL — generated images are saved locally **and** uploaded to [catbox.moe](https://catbox.moe) (no auth required, persistent URLs). The catbox URL is what gets sent to Instagram. `dall-e-3` returns a hosted URL directly and skips this step.

### Instagram posting

Posts images as a single post (1 image) or carousel (2+ images). The caption is built from the actual report content — top highlights and the stocks-mentioned section — instead of a generic placeholder. Caption language follows `whisper_language` (Chinese or English) unless `caption_template` is set explicitly.

```yaml
instagram:
  enabled: true
  # user_id is optional — auto-discovered from the token via GET /me
  api_version: v21.0
  language: ""                # optional: override (defaults to whisper_language)
  # caption_template: "🎙 {report_title}\n{date_range}\n#podcast"
  #                           # placeholders: {report_title} {date_range}
```

```bash
export INSTAGRAM_ACCESS_TOKEN="your-long-lived-token"   # add to ~/.zshenv
```

Requires an Instagram Business or Creator account linked to a Facebook App with `instagram_content_publish` permission.

### Custom report sections

```yaml
report_sections:
  - title: "Episode Summaries"
    prompt: "Summarise each episode with key arguments and conclusions."
  - title: "Key Themes"
    prompt: "What were the major themes across all episodes this week?"
  - title: "Action Items"
    prompt: "List the most important takeaways and recommendations."
```

If omitted, a built-in 4-section template is used:

1. **Episode Summaries** / 本期節目摘要
2. **Key Themes & Insights** / 重點主題與洞察
3. **Stocks Mentioned** / 本期提到的個股 — extracts every ticker, ETF, or company the hosts discuss with their stance (bullish / bearish / watch) and the reason given
4. **Takeaways & Action Items** / 重點結論與行動建議

The Chinese template is used automatically when `whisper_language: zh` (or `zh-TW` / `zh-Hant`); otherwise the English template is used. Each section is queried independently against the NotebookLM notebook.

---

## CLI reference

### Setup

```bash
psum init                     # wizard: create/edit config, optionally install cron
psum nlm-login                # authenticate with NotebookLM (browser OAuth)
```

### Running the pipeline

`[CONFIG]` is the config name, e.g. `tech` for `~/.config/psum/tech.yaml`. If omitted and multiple configs exist, you are prompted to pick one.

```bash
psum run [CONFIG]                                     # full pipeline
psum run [CONFIG] --skip-fetch                        # skip download, use existing audio
psum run [CONFIG] --skip-fetch --skip-transcribe      # upload + report only
psum run [CONFIG] --notebook-id <id>                  # report only, reuse existing notebook
psum run [CONFIG] --skip-report                       # fetch + transcribe only
psum run [CONFIG] --save-report-only                  # generate report, don't send email
psum run [CONFIG] --skip-image                        # skip image generation
psum run [CONFIG] --skip-instagram                    # skip Instagram posting
psum run [CONFIG] --folder 20260218-20260225          # target a specific date range
```

### Re-publishing an existing report

Generate cards + post to Instagram from an already-saved report (skips fetch / transcribe / NotebookLM / email entirely). Useful for re-publishing after tweaking `prompts.image`, the mascot, or any other image-side config without burning another round of NotebookLM queries.

```bash
psum publish                              # interactive: pick from all saved reports
psum publish --folder 20260428-20260429   # publish that specific folder
```

The config that originally produced the report is auto-detected from a `meta.json` file saved next to the report (it records `config`, `folder`, `report_mode`, `generated_at`). Reports created before this metadata existed are still shown in the picker; the config they belong to is inferred from which config's `source_folder` they live under.

Reports without `meta.json` are tagged with the config's name as a guess; once you re-run them through the full pipeline, the tag becomes authoritative.

### Config management

```bash
psum config list                          # list all configs in ~/.config/psum/
psum config show [CONFIG]                 # display a config (passwords redacted)
psum config create                        # wizard: create new or update existing config
psum config set [CONFIG] KEY VALUE        # set a value using dot notation
```

`[CONFIG]` is optional for `config show` and `config set` — if omitted, the picker appears (or the only config is used automatically).

```bash
# Examples
psum config show tech
psum config set tech lookback_days 3
psum config set tech email.enabled false
psum config set retention.audio_months 6   # no config name → picker
```

### Cron jobs

```bash
psum cron install [CONFIG]                # install job (reads schedule from config)
psum cron install [CONFIG] --name jobs    # named job (use different names per config)
psum cron install [CONFIG] --schedule "0 9 * * 1"   # override schedule
psum cron status                          # show all installed psum jobs
psum cron remove --name default           # remove a job by name
```

### Pre-flight check (before a scheduled run)

`psum preflight` runs an offline read-only sanity check on a config: env vars sourced from `~/.zshenv`, NotebookLM auth, OpenAI key, Instagram token expiry, feed reachability, image-hosting endpoints, and pipeline-venv imports. Useful before tomorrow's cron firing to catch expired tokens or broken feeds *before* the pipeline burns 30 minutes only to fail at the last stage.

```bash
psum preflight [CONFIG]
```

A single failed check exits non-zero and prints the exact next step (re-source shell, `psum nlm-login`, rotate token, etc.).

### Editing feeds, recipients, prompts, etc.

Everything is part of the config — there's no separate command to manage individual feeds or recipients. Edit them via `psum init` (interactive wizard) or `psum config set`:

```bash
psum init                                              # walks through every section
psum config set [CONFIG] lookback_days 3               # change one value
psum config show [CONFIG]                              # see the whole file
```

For multi-line values (e.g. tweaking `prompts.image` or the mascot description), open `~/.config/psum/<name>.yaml` directly in your editor.

---

## Multiple configs

Each config is a self-contained pipeline — different feeds, schedule, recipients, and report style.

```bash
# Create a second config
psum init   # → creates ~/.config/psum/tech.yaml

# Install a separate cron job for it
psum cron install tech --name tech-pods

# Run it manually
psum run tech

# See both jobs
psum cron status
# Status: 2 job(s) installed
#
#   Name:  default
#   Entry: 0 8 * * 0 /…/run.sh --config /…/config.yaml   # psum:default
#
#   Name:  tech-pods
#   Entry: 0 9 * * 1 /…/run.sh --config /…/tech.yaml     # psum:tech-pods
```

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
      report_20260221-20260228.txt
      card_1_episode_summaries.png     ← generated images (if enabled)
      card_2_key_themes_insights.png
  runs/
    20260221-20260228/
      status.json                      ← pipeline run status (MCP)
```

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
| `run_pipeline` | Start the pipeline in the background |
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

---

## Uninstall

```bash
psum cron remove --name default    # remove cron jobs first
rm -rf ~/.config/psum/             # CLI venv and all configs
rm -rf ./venv/                     # pipeline venv
```
