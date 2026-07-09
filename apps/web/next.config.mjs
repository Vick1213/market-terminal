const isExport = process.env.NEXT_OUTPUT === "export";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Compile the workspace TS package directly (no separate build step).
  transpilePackages: ["@market/shared"],
  // Static export for the Tauri desktop shell (apps/desktop): the app is
  // fully client-fetching (see src/lib/api.ts — absolute API_URL, no
  // rewrites/route handlers/server actions), so `output: "export"` needs no
  // other changes here. Gated behind an env var so `pnpm dev` / `pnpm build`
  // (the plain browser deployment) are completely unaffected; only
  // `pnpm build:export` (used by apps/desktop's production build) sets it.
  ...(isExport ? { output: "export" } : {}),
  // The static export is only ever consumed by the desktop bundle, which
  // ships the `market-api` sidecar on 127.0.0.1:8765 (see
  // apps/desktop/src-tauri/src/lib.rs and tauri.conf.json's
  // bundle.resources). Bake that URL in at export time so the packaged app
  // doesn't need any runtime config — src/lib/api.ts reads
  // NEXT_PUBLIC_API_URL/_WS_URL and otherwise falls back to the plain-browser
  // dev API on :8000. Only applied when those vars aren't already set
  // externally, and only for the export build — `pnpm dev` / `pnpm build`
  // (no NEXT_OUTPUT) are completely unaffected.
  ...(isExport
    ? {
        env: {
          NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8765",
          NEXT_PUBLIC_WS_URL: process.env.NEXT_PUBLIC_WS_URL ?? "ws://127.0.0.1:8765",
        },
      }
    : {}),
};

export default nextConfig;
