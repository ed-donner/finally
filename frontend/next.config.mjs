const isDev = process.env.NODE_ENV !== "production";

/** @type {import('next').NextConfig} */
const nextConfig = {
  // Production: static export, served by FastAPI from the same origin (PLAN.md section 3).
  // Dev: `next dev` cannot export, so it runs normally and proxies /api/* to the backend.
  ...(isDev
    ? {
        async rewrites() {
          const backend = process.env.BACKEND_URL || "http://localhost:8000";
          return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
        },
      }
    : { output: "export" }),
  // Streaming (SSE) responses must not be buffered by gzip in the dev proxy.
  compress: false,
  reactStrictMode: true,
};

export default nextConfig;
