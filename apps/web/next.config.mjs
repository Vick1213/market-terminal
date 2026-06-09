/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Compile the workspace TS package directly (no separate build step).
  transpilePackages: ["@market/shared"],
};

export default nextConfig;
