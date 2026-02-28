#!/usr/bin/env node
"use strict";

const { spawn } = require("child_process");
const path = require("path");

const bin = path.join(require("os").homedir(), ".config", "psum", "venv", "bin", "psum-mcp");
const proc = spawn(bin, process.argv.slice(2), { stdio: "inherit" });
proc.on("close", (code) => process.exit(code ?? 0));
