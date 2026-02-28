#!/usr/bin/env node
"use strict";
/**
 * postinstall.js
 *
 * Run automatically after `npm install -g`.
 * Creates the CLI venv at ~/.config/psum/venv (stable across reinstalls)
 * and installs psum + psum-mcp entry points via `pip install .`.
 *
 * Re-runs only when the package version changes, so reinstalls are fast.
 *
 * The pipeline venv (venv/) requires Python 3.9+ and heavier dependencies
 * (faster-whisper, etc.) — set it up separately inside the project dir:
 *   python3 -m venv venv && venv/bin/pip install -r requirements.txt
 */

const { execSync, execFileSync } = require("child_process");
const path = require("path");
const fs   = require("fs");
const os   = require("os");

const ROOT         = path.join(__dirname, "..");
const PSUM_DIR     = path.join(os.homedir(), ".config", "psum");
const VENV         = path.join(PSUM_DIR, "venv");
const PSUM_BIN     = path.join(VENV, "bin", "psum");
const VERSION_FILE = path.join(PSUM_DIR, ".cli_version");

const pkg         = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));
const CURRENT_VER = pkg.version;

// ── Skip if already installed at this version ─────────────────────────────
if (fs.existsSync(PSUM_BIN) && fs.existsSync(VERSION_FILE)) {
  const installedVer = fs.readFileSync(VERSION_FILE, "utf8").trim();
  if (installedVer === CURRENT_VER) {
    console.log(`psum: CLI v${CURRENT_VER} already installed at ${VENV}, skipping.`);
    process.exit(0);
  }
  console.log(`psum: Upgrading CLI from v${installedVer} to v${CURRENT_VER}…`);
}

// ── Find Python 3.10+ ─────────────────────────────────────────────────────
function findPython() {
  const candidates = [
    "python3.14", "python3.13", "python3.12", "python3.11", "python3.10",
    "python3", "python",
  ];
  for (const cmd of candidates) {
    try {
      const out = execSync(`${cmd} --version 2>&1`, { encoding: "utf8" }).trim();
      const m = out.match(/Python 3\.(\d+)/);
      if (m && parseInt(m[1], 10) >= 10) return cmd;
    } catch { /* not found */ }
  }
  return null;
}

const python = findPython();
if (!python) {
  console.error(
    "\npsum postinstall: Python 3.10+ is required but was not found.\n" +
    "Install it from https://www.python.org/downloads/ then run:\n" +
    "  npm install -g podcast-summary\n"
  );
  process.exit(1);
}

// ── Create venv and install ───────────────────────────────────────────────
fs.mkdirSync(PSUM_DIR, { recursive: true });
console.log(`\npsum: Setting up Python venv at ${VENV} with ${python}…`);
try {
  execFileSync(python, ["-m", "venv", VENV], { stdio: "inherit", cwd: ROOT });

  const pip = path.join(VENV, "bin", "pip");
  execFileSync(pip, ["install", "."], { stdio: "inherit", cwd: ROOT });
  execFileSync(pip, ["install", "notebooklm-mcp-cli"], { stdio: "inherit", cwd: ROOT });

  fs.writeFileSync(VERSION_FILE, CURRENT_VER + "\n", "utf8");

  // ── Set project_root in config if not already pointing to a valid location ─
  const configPath = path.join(PSUM_DIR, "config.yaml");
  let needsProjectRoot = true;
  if (fs.existsSync(configPath)) {
    const content = fs.readFileSync(configPath, "utf8");
    const match = content.match(/^project_root:\s*(.+)$/m);
    if (match) {
      const existing = match[1].trim().replace(/^['"]|['"]$/g, "");
      if (fs.existsSync(path.join(existing, "pipeline.py"))) {
        needsProjectRoot = false;
      }
    }
  }
  if (needsProjectRoot) {
    execFileSync(PSUM_BIN, ["config", "set", "project_root", ROOT], { stdio: "inherit" });
    console.log(`psum: Set project_root to ${ROOT}`);
  }

  // ── Set nlm_path if not already pointing to a working binary ─────────────
  const nlmBin = path.join(VENV, "bin", "nlm");
  let needsNlmPath = true;
  if (fs.existsSync(configPath)) {
    const content = fs.readFileSync(configPath, "utf8");
    const match = content.match(/^nlm_path:\s*(.+)$/m);
    if (match) {
      const existing = match[1].trim().replace(/^['"]|['"]$/g, "");
      if (fs.existsSync(existing)) needsNlmPath = false;
    }
  }
  if (needsNlmPath && fs.existsSync(nlmBin)) {
    execFileSync(PSUM_BIN, ["config", "set", "nlm_path", nlmBin], { stdio: "inherit" });
    console.log(`psum: Set nlm_path to ${nlmBin}`);
  }

  console.log(`\npsum: Setup complete (v${CURRENT_VER}). Run \`psum --help\` to get started.\n`);
} catch (err) {
  console.error("\npsum postinstall failed:", err.message);
  console.error("You can retry manually:\n");
  console.error(`  ${python} -m venv ${VENV}`);
  console.error(`  ${VENV}/bin/pip install ${ROOT}`);
  process.exit(1);
}
