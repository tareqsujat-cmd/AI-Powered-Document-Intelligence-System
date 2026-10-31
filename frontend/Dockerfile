FROM node:26-alpine AS base
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app

# Dev dependencies are required by the build, so NODE_ENV must stay unset here.
FROM base AS deps
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM base AS build
# NEXT_PUBLIC_* values are inlined into the browser bundle by `next build`, so
# this must be a build argument. Setting it at container runtime has no effect.
# It has to be a URL the user's browser can reach, not a compose service name.
ARG NEXT_PUBLIC_API_URL
# ":-" also covers an empty value, which `??` in ApiStatus would not fall back on.
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL:-http://localhost:8000}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM base AS runtime
ENV NODE_ENV=production
ENV PORT=3000
ENV HOSTNAME=0.0.0.0
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
COPY --from=build --chown=node:node /app/public ./public
USER node
EXPOSE 3000
CMD ["node", "server.js"]
