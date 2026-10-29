# Orches web client (Nuxt). Build:  docker build -t orches-web web
# Run:  docker run --rm -p 3000:3000 \
#         -e NUXT_PUBLIC_API_BASE_URL=https://api.example.com orches-web
#
# NUXT_PUBLIC_API_BASE_URL is the backend origin the *browser* calls directly
# (bare https origin; http only for loopback development). Leave it unset to use
# the same-origin /api proxy instead and point NUXT_API_PROXY_URL at a backend
# reachable *from inside the container* (loopback-only validation applies).

FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production \
    NITRO_HOST=0.0.0.0 \
    NITRO_PORT=3000
COPY --from=build /app/.output ./.output
EXPOSE 3000
USER node
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD wget -q -O /dev/null http://127.0.0.1:3000/login || exit 1
CMD ["node", ".output/server/index.mjs"]
