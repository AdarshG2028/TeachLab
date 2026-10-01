import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Keep builds light on small/Windows dev machines (parallel page-data workers can exhaust resources).
  experimental: { cpus: 1 },
};

export default nextConfig;
