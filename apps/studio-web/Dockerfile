# Node 24 LTS, exact patch and Debian variant, pinned by the multi-arch image index digest.
# Keep in step with /.node-version (apps/api/tests/test_node_version_policy.py enforces it).
ARG NODE_IMAGE=node:24.22.3-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54

# --- build stage ---
FROM ${NODE_IMAGE} AS build
ARG SOURCE_SHA
WORKDIR /app
COPY apps/studio-web/package.json apps/studio-web/package-lock.json ./
RUN npm ci
COPY apps/studio-web .
RUN npm run build
RUN printf '%s' "$SOURCE_SHA" > dist/source-sha

# --- dev stage ---
FROM ${NODE_IMAGE} AS dev
RUN groupadd --gid 1000 appuser || true && useradd --uid 1000 --gid 1000 --create-home appuser || true
WORKDIR /app
COPY apps/studio-web/package.json apps/studio-web/package-lock.json ./
RUN npm ci
COPY --chown=appuser:appuser apps/studio-web .
USER appuser
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]

# --- runtime stage ---
FROM nginxinc/nginx-unprivileged:alpine AS runtime
ARG SOURCE_SHA
LABEL org.opencontainers.image.revision="${SOURCE_SHA}"
COPY apps/studio-web/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --chown=101:101 --from=build /app/dist /usr/share/nginx/html
COPY --chown=101:101 apps/studio-web/docker-entrypoint.d/40-release-identity.sh /docker-entrypoint.d/40-release-identity.sh
RUN chmod +x /docker-entrypoint.d/40-release-identity.sh
# The nginx docker-entrypoint runs envsubst on /etc/nginx/templates/ at startup.
# NGINX_ENVSUBST_FILTER limits substitution to API_KEY only, preserving nginx
# variables ($host, $remote_addr, etc.) in the template.
ENV API_KEY=""
ENV NGINX_ENVSUBST_FILTER="API_KEY"
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
