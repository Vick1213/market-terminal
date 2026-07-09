#!/usr/bin/env bash
# Production build wrapper — see apps/desktop/README.md "Production build".
#
# `tauri.conf.json`'s `beforeBuildCommand` already runs the web static export
# (`pnpm --filter @market/web build:export`) before Tauri packages the app,
# so this script's only job is argument hygiene:
#
# `pnpm build -- --debug` is the documented pnpm/npm way to forward flags
# through a `package.json` script, but pnpm does so by textually appending
# " -- --debug" (literal `--` included) to whatever command the script runs
# (verified empirically — this is not a guess). Tauri's CLI parses `tauri
# build [OPTIONS] [ARGS]...`, where `--` explicitly marks the start of the
# trailing `[ARGS]...` (extra args forwarded verbatim to the cargo runner),
# NOT tauri build's own `-d/--debug` flag. So an unfiltered `tauri build --
# --debug` sends `--debug` to cargo, which rejects it ("unexpected argument
# '--debug' found"), silently building in release mode instead of debug.
#
# Strip one leading bare `--` before forwarding so both `pnpm build --debug`
# and `pnpm build -- --debug` produce the intended `tauri build --debug`.
set -euo pipefail
if [ "${1-}" = "--" ]; then
  shift
fi
exec tauri build "$@"
