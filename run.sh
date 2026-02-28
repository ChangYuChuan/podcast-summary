#!/bin/zsh
# run.sh — Wrapper script for the podcast-summary pipeline.
#
# Sources ~/.zprofile so that EMAIL_SMTP_PASSWORD and other env vars
# are available even when invoked from cron (which runs with a bare environment).
#
# All arguments are forwarded to pipeline.py — use --config to select a config:
#   psum cron install --name my-job --schedule "0 8 * * 0"
#   (generates: 0 8 * * 0 /path/to/run.sh --config /path/to/config.yaml)

source ~/.zprofile

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

LOG_FILE="$SCRIPT_DIR/logs/pipeline.log"
mkdir -p "$SCRIPT_DIR/logs"

echo "======================================" >> "$LOG_FILE"
echo "Run started: $(date)" >> "$LOG_FILE"
echo "======================================" >> "$LOG_FILE"

"$SCRIPT_DIR/venv/bin/python3" "$SCRIPT_DIR/pipeline.py" "$@" 2>&1 | tee -a "$LOG_FILE"

EXIT_CODE=${PIPESTATUS[0]}
echo "" >> "$LOG_FILE"
echo "Run finished: $(date) (exit $EXIT_CODE)" >> "$LOG_FILE"

exit $EXIT_CODE
