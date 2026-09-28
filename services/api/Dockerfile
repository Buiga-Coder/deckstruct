ARG NODE_IMAGE=public.ecr.aws/docker/library/node:22-bookworm-slim
FROM ${NODE_IMAGE} AS build
RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/api/package*.json ./
RUN npm ci --omit=dev
FROM ${NODE_IMAGE}
ENV NODE_ENV=production PORT=3000 DB_PATH=/data/deckstruct.db UPLOAD_DIR=/data/uploads
WORKDIR /app
COPY --from=build /app/node_modules ./node_modules
COPY services/api/ ./
RUN mkdir -p /data/uploads && chown -R node:node /data
USER node
CMD ["node", "server.js"]
