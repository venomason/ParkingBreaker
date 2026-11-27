# Customer website. The API is reached through the same-origin /api proxy.
FROM node:24.11.0-slim AS build
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web ./
ARG PB_API_ORIGIN=http://api:8000
ARG PB_PROVIDER_MODE=fake
ARG NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY=
ENV PB_API_ORIGIN=$PB_API_ORIGIN PB_PROVIDER_MODE=$PB_PROVIDER_MODE NEXT_PUBLIC_LIVE_API=1 \
    NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY=$NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY NEXT_TELEMETRY_DISABLED=1
# A private upload may contain root-only modes. The final server runs as node.
RUN find . -path ./node_modules -prune -o -exec chmod a+rX {} + && npm run build

FROM node:24.11.0-slim
ARG PB_BUILD_COMMIT
LABEL org.opencontainers.image.revision=$PB_BUILD_COMMIT
WORKDIR /app/web
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
COPY --from=build /app/web ./
USER node
EXPOSE 3000
CMD ["npx", "next", "start", "-p", "3000"]
