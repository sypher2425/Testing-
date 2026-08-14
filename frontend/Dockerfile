FROM node:20-slim AS deps
WORKDIR /app
COPY package.json ./
RUN npm install

FROM node:20-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ARG NEXT_PUBLIC_API_BASE_URL=
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
ARG API_PROXY_TARGET=http://api:8000
ENV API_PROXY_TARGET=$API_PROXY_TARGET
# Inlined into the client bundle at build time, so it must be a build arg —
# setting it only at runtime would not reach the browser.
ARG NEXT_PUBLIC_MAX_UPLOAD_MB=61440
ENV NEXT_PUBLIC_MAX_UPLOAD_MB=$NEXT_PUBLIC_MAX_UPLOAD_MB
RUN npm run build

FROM node:20-slim AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

EXPOSE 3000
CMD ["node", "server.js"]
