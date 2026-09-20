# Playwright test runner. Kept separate from the production image so browser dependencies never
# ship with the app (PLAN.md section 12). The image tag MUST match the @playwright/test version
# pinned in package.json, or Playwright cannot find its browsers.
FROM mcr.microsoft.com/playwright:v1.63.0-noble

WORKDIR /work

# Dependencies first so this layer is cached until the lockfile changes.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .

# BASE_URL is provided by docker-compose.test.yml (http://app:8000).
CMD ["npx", "playwright", "test"]
