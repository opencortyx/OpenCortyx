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
        destination: `${process.env.NEXT_PUBLIC_API_URL}/api/v2/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
