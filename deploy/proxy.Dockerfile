# The TLS reverse proxy with the built frontend (docs/deployment.md). Build from the
# repository root:
#   docker build -f deploy/proxy.Dockerfile -t inquest-proxy .
# Pin both base images by digest for production builds.
FROM node:22-alpine AS web
WORKDIR /web
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

FROM nginx:1.30-alpine
# Rendered at start by the image's envsubst (INQUEST_* variables only, see the template).
ENV NGINX_ENVSUBST_FILTER=^INQUEST_
RUN rm /etc/nginx/conf.d/default.conf
COPY deploy/nginx/inquest.conf.template /etc/nginx/templates/inquest.conf.template
COPY --from=web /web/dist /usr/share/nginx/html
