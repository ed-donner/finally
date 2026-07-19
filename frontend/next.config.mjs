/** @type {import('next').NextConfig} */
const isDev = process.env.NODE_ENV === 'development';

// In dev, the backend runs separately on :8000. We proxy /api/* to it so the
// browser always talks to the same origin (matching production, where FastAPI
// serves this static export and the API from one port). Rewrites are a dev-time
// feature and are intentionally not baked into the static export.
const BACKEND_ORIGIN = process.env.BACKEND_ORIGIN || 'http://localhost:8000';

const nextConfig = {
  // Static export — FastAPI serves the built files from a static/ directory in prod.
  output: 'export',
  // Static export can't optimize images at runtime.
  images: { unoptimized: true },
  // Emit trailing-slash directories so the export maps cleanly onto a static file server.
  trailingSlash: true,
  reactStrictMode: true,
  ...(isDev
    ? {
        async rewrites() {
          return [
            { source: '/api/:path*', destination: `${BACKEND_ORIGIN}/api/:path*` },
          ];
        },
      }
    : {}),
};

export default nextConfig;
