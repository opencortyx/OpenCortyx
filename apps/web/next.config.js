/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  images: { unoptimized: true },
  reactStrictMode: true,
  poweredByHeader: false,
  compress: true,
  productionBrowserSourceMaps: false,
  onDemandEntries: { maxInactiveAge: 20 * 1000, pagesBufferLength: 2 },
  experimental: {
    typedRoutes: true,
    optimizePackageImports: ["framer-motion", "d3", "d3-force", "react-markdown", "next-themes"],
  },
  async rewrites() {
    return [
      {
        source: "/api/backend/:path*",
        // Same default as the Dockerfile and compose.yaml; without one, a plain `next build` (CI)
        // fails on an "undefined/api/v2" rewrite.
        destination: `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/api/v2/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
