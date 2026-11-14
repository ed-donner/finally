import type { NextConfig } from "next";

const isDev = process.env.NODE_ENV === "development";
const backend = process.env.FINALLY_BACKEND_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
  // Rewrites only apply to `next dev`; the static export is served same-origin by FastAPI.
  ...(isDev
    ? {
        async rewrites() {
          return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
        },
      }
    : {}),
};

export default nextConfig;
