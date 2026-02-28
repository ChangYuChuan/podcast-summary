#!/usr/bin/env node
"use strict";
/**
 * preuninstall.js
 *
 * Runs automatically before `npm uninstall -g podcast-summary`.
 * Removes all psum cron jobs so they don't point to deleted files.
 */

const { execSync } = require("child_process");

const CRON_MARKER_PREFIX = "# psum:";

function removeCronJobs() {
  try {
    const current = execSync("crontab -l 2>/dev/null", {
      encoding: "utf8",
    }).trim();

    if (!current.includes(CRON_MARKER_PREFIX)) {
      return; // nothing to remove
    }

    const cleaned = current
      .split("\n")
      .filter((line) => !line.includes(CRON_MARKER_PREFIX))
      .join("\n")
      .trim();

    if (cleaned) {
      execSync(`echo "${cleaned}" | crontab -`);
    } else {
      execSync("crontab -r 2>/dev/null || true", { shell: true });
    }

    console.log("psum: removed cron job(s).");
  } catch {
    // crontab may not exist — not an error
  }
}

removeCronJobs();

console.log(
  "\npsum: uninstalled.\n" +
  "  If you had Claude Desktop configured, remove the 'podcast-summary'\n" +
  "  entry from claude_desktop_config.json.\n"
);
