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
  ...(process.env.NEXT_OUTPUT === "export" ? { output: "export" } : {}),
};

export default nextConfig;
