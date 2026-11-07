import type { NextConfig } from "next";

const apiBaseUrl = process.env.JOBCOCKPIT_API_BASE_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  experimental: {
    // ATS imports can still be long-running; document generation uses durable queues.
    proxyTimeout: 4_800_000
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiBaseUrl}/api/:path*`
      }
    ];
  }
};

export default nextConfig;
