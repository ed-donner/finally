import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  // Images: disable optimization for static export
  images: {
    unoptimized: true,
  },
};

export default nextConfig;
