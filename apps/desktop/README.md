# apps/desktop — Tauri v2 desktop shell

Wraps the existing Next.js UI (`apps/web`) in a native window using
[Tauri v2](https://v2.tauri.app/). Dev mode, native multi-window popouts, and
the **production build path** (static-exporting `apps/web` and packaging it
with `tauri build`) are wired up. Bundling a Python/FastAPI sidecar into the
app and installer signing/notarization/auto-update are still out of scope —
see "What's NOT wired up yet" below.

## Prerequisites

- Rust toolchain (`rustc`/`cargo`) on `PATH`. If installed via rustup at
  `~/.cargo/bin`, make sure that's on `PATH` in the shell you use:
  `export PATH="$HOME/.cargo/bin:$PATH"`.
- Node + pnpm (already required for the rest of the monorepo).
- `pnpm install` at the repo root at least once (installs `@tauri-apps/cli`
  here and `@tauri-apps/api` in `apps/web`).

## How to run

This app does **not** start the web or API servers itself — `beforeDevCommand`
is intentionally left empty in `src-tauri/tauri.conf.json`. Start them
yourself first, the same way you would for browser-only development:

**Terminal 1** — from the repo root:

```sh
honcho start
```

This runs both `apps/api` (FastAPI on `127.0.0.1:8000`) and `apps/web` (Next.js
dev server on `http://localhost:3000`) per the root `Procfile`.

**Terminal 2** — once `http://localhost:3000` is up:

```sh
cd apps/desktop && pnpm dev
# or from the repo root: pnpm --filter @market/desktop dev
```

`pnpm dev` runs `tauri dev`, which points its webview at
`devUrl: http://localhost:3000` (see `src-tauri/tauri.conf.json`) and opens a
native window titled "Market Terminal" (1440x900, resizable). The first Rust
compile takes several minutes; subsequent runs are fast (incremental).

For the production build (`pnpm build`), see "Production build" below.

## Production build

```sh
cd apps/desktop && pnpm build          # release profile (default) — slower, optimized
cd apps/desktop && pnpm build --debug  # debug profile — faster, unoptimized, for smoke-testing
```

This runs `tauri build`, which:

1. Runs `build.beforeBuildCommand` (see `src-tauri/tauri.conf.json`):
   `pnpm --filter @market/web build:export`. This statically exports
   `apps/web` to `apps/web/out` (see `apps/web/next.config.mjs` /
   `package.json`'s `build:export` script — gated behind `NEXT_OUTPUT=export`
   so the normal `pnpm dev` / `pnpm build` browser deployment of `apps/web` is
   completely unaffected). The app is fully client-fetching (see
   `apps/web/src/lib/api.ts`), so the static export needs no rewrites, route
   handlers, or server actions to work.
2. Packages `apps/web/out` (via `build.frontendDist: "../../web/out"`) into a
   native app bundle (`.app` on macOS, plus a `.dmg` if code signing/hdiutil
   succeeds) under `src-tauri/target/{debug,release}/bundle/`.

You do **not** need to manually run the web export first — step 1 does it for
you. If you want to do it explicitly (e.g. to inspect `apps/web/out` without
invoking Rust), the two-step is:

```sh
pnpm --filter @market/web build:export   # from repo root, or `cd apps/web && pnpm build:export`
cd apps/desktop && pnpm exec tauri build --debug
```

**`frontendDist` path note**: `src-tauri/tauri.conf.json`'s `build.frontendDist`
is `"../../web/out"` — resolved relative to `src-tauri/` (where
`tauri.conf.json` lives), i.e. `apps/desktop/src-tauri/../../web/out` →
`apps/web/out`. (A single `../web/out` — one level too shallow — resolves to
the nonexistent `apps/desktop/web/out` and makes every `tauri build` fail with
"Unable to find your web assets"; confirmed by the exact absolute path Tauri
itself prints in that error.)

**`pnpm build -- --debug` arg-forwarding gotcha**: pnpm's documented way to
forward flags through a `package.json` script is `pnpm <script> -- <args>`,
but pnpm does this by literally appending `" -- --debug"` (the `--` included)
to the end of whatever command the script runs. Tauri's CLI parses `tauri
build [OPTIONS] [ARGS]...`, where a `--` marks the start of the trailing
`[ARGS]...` — extra args forwarded verbatim to the `cargo` runner — **not**
`tauri build`'s own `-d/--debug` flag. An unfiltered `tauri build -- --debug`
therefore sends `--debug` straight to `cargo build`, which rejects it
(`error: unexpected argument '--debug' found`) and the build fails. `"build"`
in `package.json` is `bash scripts/build.sh` (not a bare `"tauri build"`) so
that a leading `--` can be stripped before forwarding — both
`pnpm build --debug` and `pnpm build -- --debug` correctly produce a debug
build. See `scripts/build.sh` for the (short) implementation.

**Verified in this environment**: `pnpm build -- --debug` runs the full
pipeline (web export → `cargo build` → `.app` bundling) successfully and
produces `src-tauri/target/debug/bundle/macos/Market Terminal.app` (confirmed
Mach-O binary + `Info.plist` + `icon.icns` present). The subsequent `.dmg`
step (`bundle_dmg.sh`) additionally hung/failed in this sandboxed, no-GUI-
session test environment — it shells out to `osascript` to arrange Finder
icon positions inside a temporary mounted volume, which needs an interactive
Finder to talk to. That's an environment limitation, not a config bug: on a
normal logged-in Mac this should just work. To reproduce the clean, fast
result verified here (skip the `.dmg`, keep the `.app`):

```sh
pnpm build -- --bundles app --debug
```

which finished cleanly (`Finished 1 bundle at: .../bundle/macos/Market Terminal.app`,
exit code 0, no leftover processes).

## Native multi-window popouts

The web app already supports popping a panel out into its own window
(`apps/web/src/hooks/usePopouts.ts`): `openPopout(id)` / `bringBack(id)`,
backed by `BroadcastChannel("market:popouts")` + `localStorage` sync. Plain
`window.open` doesn't produce a real OS window inside a Tauri webview, so
`usePopouts.ts` now branches at runtime:

- **Tauri detected** (`"__TAURI_INTERNALS__" in window`, checked in
  `isTauri()`): dynamically imports `@tauri-apps/api/webviewWindow` and
  creates (or focuses, if already open) a native `WebviewWindow` labeled
  `popout-<id>` pointed at `/popout?panel=<id>`, ~720x560. `@tauri-apps/api`
  is **only** imported inside this branch (dynamic `import()`), so the plain
  browser build never bundles/evaluates Tauri's IPC glue.
- **Not Tauri** (regular browser): unchanged — `window.open` with a named
  window, exactly as before.

`/popout` is a single static page (`apps/web/src/app/popout/page.tsx`, no
`[panelId]` dynamic route segment) that reads the panel id from a `?panel=`
query string client-side via `useSearchParams()`. This is deliberate for the
static export (see "Production build" below): a static export can only
pre-render dynamic route segments it knows about ahead of time, but query
strings aren't part of route matching, so the one exported
`out/popout.html` works for every panel id — known or unknown — and Tauri's
own asset resolver serves it for a bare `/popout` request (it falls back from
an exact path match to `<path>.html` before trying `<path>/index.html` or
`index.html` — see `tauri-2.11.5/src/manager/mod.rs::get_asset` in the Cargo
registry cache). The "Unknown panel" error branch in `popout/page.tsx` stays
reachable at runtime for any id that isn't in `panelRegistry.tsx` (e.g. a
stale `localStorage` entry from a previous session), exactly like it did as a
dynamic route in dev.

Same-origin reasoning for `BroadcastChannel`/`localStorage` sync across native
windows: in dev, `devUrl` points the whole app (main window **and** any
`WebviewWindow` opened with a relative `url`) at the Next.js dev server
(`http://localhost:3000`). A relative popout URL like `/popout?panel=<id>`
resolves against that same `devUrl`, so every native popout window loads from
the exact same origin as the main window — same as a `window.open`'d browser
tab. In production, `frontendDist` serves the exact same single `/popout`
page for every id, so this holds there too. No extra IPC bridging should be
needed for the cross-window sync to keep working. This reasoning is
documented in code comments in `usePopouts.ts`; **actual multi-window runtime
testing under `tauri dev` was not performed this round** (scope note from the
task, and see caveat below).

### Capabilities

`src-tauri/capabilities/default.json` grants the main window's webview the
extra permissions needed to create/focus native popout windows from JS, on
top of `core:default`:

- `core:webview:allow-create-webview-window` — lets `new WebviewWindow(...)`
  succeed (the command the JS constructor invokes).
- `core:window:allow-set-focus` — lets `.setFocus()` bring an existing popout
  to the front (used both when re-opening an already-open popout and in
  `bringBack`).
- `core:window:allow-close` — granted for completeness/future use (e.g. a
  popout closing itself or being closed programmatically); not required by
  the current code path.

None of these are in `core:default` — they're capability-gated in Tauri v2
because window/webview creation and focus-stealing are considered sensitive.

## Configuration choices (`src-tauri/tauri.conf.json`)

- `identifier`: `com.market.terminal` (placeholder pending final product
  naming; `productName`: "Market Terminal", also a placeholder).
- `build.devUrl`: `http://localhost:3000` — matches `apps/web`'s dev server.
- `build.beforeDevCommand`: empty string `""`. The dev servers are started
  separately via `honcho start`; Tauri should not spawn them itself.
  (Not runtime-verified under `tauri dev` this round — see caveat below.)
- `build.beforeBuildCommand`: `"pnpm --filter @market/web build:export"` —
  runs before every `tauri build`, so the static export always exists (and is
  fresh) by the time Tauri packages it. See "Production build" above.
- `build.frontendDist`: `"../../web/out"` — resolved relative to `src-tauri/`
  (where `tauri.conf.json` lives), so this points at `apps/web/out`, the
  static export's output directory (see `apps/web/next.config.mjs`). Note the
  double `../../`: a single `../web/out` resolves to the nonexistent
  `apps/desktop/web/out` one level too shallow, and makes every `tauri build`
  fail with "Unable to find your web assets" (confirmed by the exact absolute
  path Tauri itself prints in that error message).
- Main window: `label: "main"` (matches the capability file's `"windows":
  ["main"]`), `1440x900`, `minWidth/minHeight: 1024x700`, resizable, title
  "Market Terminal".

## What's NOT wired up yet

- **No Python/FastAPI sidecar bundling.** `apps/api` still runs as its own
  process; nothing here packages or spawns it. This means:
  **the packaged app cannot fetch any data until `apps/api`'s CORS config
  allows the Tauri production origin.** `apps/web/src/lib/api.ts` calls the
  API via an absolute URL (`NEXT_PUBLIC_API_URL ?? http://127.0.0.1:8000`),
  so from inside the packaged app those `fetch()` calls originate from
  `tauri://localhost` (macOS/Linux) or `http://tauri.localhost` (Windows) —
  not `http://localhost:3000` like the dev server. **Needed later in
  `apps/api`:** add those origins to whatever CORS allow-list
  `apps/api` uses (e.g. FastAPI's `CORSMiddleware`) alongside the existing dev
  origin, or the packaged app will get CORS errors on every API call. Not
  done here — `apps/api` is another agent's concurrent work; this is a note,
  not a change.
- **No installers / code signing / notarization / auto-update.** `tauri
  build`'s default `bundle.targets: "all"` + no signing identity means macOS
  bundles are ad-hoc/unsigned (fine for local dev/smoke-testing, not for
  distribution). All of installers/signing/notarization/auto-update are later
  per `PRODUCT.md` M2/M6.
- **`tauri dev` was not runtime-tested this round** (no GUI smoke test was
  performed against the real `apps/web` dev server in this environment — see
  the task's explicit scope note that runtime multi-window testing isn't
  required yet). The `beforeDevCommand: ""` / `devUrl` config was verified by
  static inspection and `cargo check` only. First real run should be treated
  as the first live test of that assumption. (`tauri build` — the production
  path — *was* runtime-verified this round; see "Production build" above.)
- **Multi-window popout runtime behavior** (native `WebviewWindow` creation,
  cross-window `BroadcastChannel`/`localStorage` sync) was reasoned about but
  not smoke-tested under a live `tauri dev`/`tauri build` GUI session — same
  caveat as above, now also applying to the new `/popout?panel=<id>` query
  scheme.
