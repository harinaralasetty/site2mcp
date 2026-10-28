#!/usr/bin/env node
"use strict";

const { spawnSync } = require("node:child_process");
const { version } = require("../package.json");
const packageSpec = `site2mcp==${version}`;
const sourceSetup = "https://github.com/harinaralasetty/site2mcp#quick-start";
const hint = `site2mcp: install uv or pipx with Python 3.11+, then run uvx ${packageSpec}; if this release is not on PyPI yet, use the source setup at ${sourceSetup}`;

for (const [command, prefix] of [["uvx", []], ["pipx", ["run"]]]) {
  const probe = spawnSync(command, ["--version"], { stdio: "ignore" });
  if (probe.error || probe.status !== 0) continue;

  const child = spawnSync(command, [...prefix, packageSpec, ...process.argv.slice(2)], {
    stdio: "inherit",
  });
  if (child.error) continue;
  if (child.signal) {
    process.kill(process.pid, child.signal);
    process.exit(1);
  }
  if (child.status !== 0) {
    console.error(`site2mcp: command failed; check the error above. If ${packageSpec} could not be fetched from PyPI, use the source setup at ${sourceSetup}`);
  }
  process.exit(child.status ?? 1);
}

console.error(hint);
process.exit(1);
