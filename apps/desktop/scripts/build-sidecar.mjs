#!/usr/bin/env node
/**
 * Builds the FastAPI backend (apps/api) into a PyInstaller onedir sidecar and
 * stages it where the Tauri/Rust side expects it: a directory named
 * `market-api` (containing the `market-api` / `market-api.exe` executable)
 * at apps/desktop/src-tauri/sidecar/market-api/. Do not change that path or
 * the executable name — it's a fixed contract with the Rust side.
 *
 * Steps:
 *   1. `uv run --group bundle pyinstaller --noconfirm sidecar.spec`, run with
 *      cwd = apps/api (see apps/api/sidecar.spec + pyproject.toml's `bundle`
 *      dependency group).
 *   2. Replace apps/desktop/src-tauri/sidecar/market-api/ with the freshly
 *      built apps/api/dist/market-api/ onedir (recursive copy, preserving
 *      executable permissions).
 *
 * Usage (from anywhere):  node apps/desktop/scripts/build-sidecar.mjs
 * All paths are resolved relative to this script file's location, not
 * process.cwd(), so it works regardless of where it's invoked from.
 */

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
// apps/desktop/scripts -> apps/desktop -> apps -> <repo root>
const REPO_ROOT = path.resolve(SCRIPT_DIR, "..", "..", "..");
const API_DIR = path.join(REPO_ROOT, "apps", "api");
const BUILT_ONEDIR = path.join(API_DIR, "dist", "market-api");
const STAGE_DIR = path.join(REPO_ROOT, "apps", "desktop", "src-tauri", "sidecar");
const STAGED_ONEDIR = path.join(STAGE_DIR, "market-api");

const EXE_NAME = process.platform === "win32" ? "market-api.exe" : "market-api";

function log(msg) {
  console.log(`[build-sidecar] ${msg}`);
}

function fail(msg) {
  console.error(`[build-sidecar] ERROR: ${msg}`);
  process.exit(1);
}

function run(cmd, args, opts) {
  log(`running: ${cmd} ${args.join(" ")} (cwd=${opts.cwd})`);
  const result = spawnSync(cmd, args, { stdio: "inherit", ...opts });
  if (result.error) {
    fail(`failed to launch "${cmd}": ${result.error.message}`);
  }
  if (result.status !== 0) {
    fail(`"${cmd} ${args.join(" ")}" exited with code ${result.status}`);
  }
}

function main() {
  log(`repo root:      ${REPO_ROOT}`);
  log(`api dir:        ${API_DIR}`);
  log(`onedir output:  ${BUILT_ONEDIR}`);
  log(`stage target:   ${STAGED_ONEDIR}`);

  if (!fs.existsSync(path.join(API_DIR, "sidecar.spec"))) {
    fail(`sidecar.spec not found at ${path.join(API_DIR, "sidecar.spec")}`);
  }

  // --- 1. PyInstaller build -------------------------------------------------
  log("building PyInstaller onedir (this can take a couple of minutes, torch is heavy)...");
  const uvBin = process.platform === "win32" ? "uv.exe" : "uv";
  run(uvBin, ["run", "--group", "bundle", "pyinstaller", "--noconfirm", "sidecar.spec"], {
    cwd: API_DIR,
  });

  if (!fs.existsSync(BUILT_ONEDIR)) {
    fail(`PyInstaller reported success but ${BUILT_ONEDIR} does not exist`);
  }
  const builtExe = path.join(BUILT_ONEDIR, EXE_NAME);
  if (!fs.existsSync(builtExe)) {
    fail(`built onedir is missing the expected executable: ${builtExe}`);
  }
  log(`PyInstaller build complete: ${BUILT_ONEDIR}`);

  // --- 2. Stage into apps/desktop/src-tauri/sidecar/market-api/ ------------
  log(`staging onedir -> ${STAGED_ONEDIR}`);
  fs.mkdirSync(STAGE_DIR, { recursive: true });
  if (fs.existsSync(STAGED_ONEDIR)) {
    log(`removing previously staged onedir at ${STAGED_ONEDIR}`);
    fs.rmSync(STAGED_ONEDIR, { recursive: true, force: true });
  }
  // fs.cpSync preserves file mode (incl. the executable bit) on both POSIX
  // and Windows — verified empirically; no need to shell out to `cp -R` /
  // robocopy for this.
  fs.cpSync(BUILT_ONEDIR, STAGED_ONEDIR, { recursive: true });

  const stagedExe = path.join(STAGED_ONEDIR, EXE_NAME);
  if (!fs.existsSync(stagedExe)) {
    fail(`staging finished but ${stagedExe} is missing`);
  }
  const mode = fs.statSync(stagedExe).mode;
  if (process.platform !== "win32" && !(mode & 0o111)) {
    // Belt-and-suspenders: fs.cpSync has preserved the exec bit in testing,
    // but if some environment ever loses it, restore it rather than ship a
    // sidecar the Rust side can't spawn.
    log("staged executable lost its exec bit — restoring it");
    fs.chmodSync(stagedExe, mode | 0o111);
  }

  log("done.");
  log(`staged sidecar: ${STAGED_ONEDIR}`);
  log(`executable:     ${stagedExe}`);
}

main();
