# --- Stage 1: build the bgutil PO-token server (Node/TypeScript) ---
FROM node:22-bookworm-slim AS potserver
ARG BGUTIL_VERSION=2.0.2
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt
RUN git clone --depth 1 --single-branch --branch ${BGUTIL_VERSION} \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git bgutil \
 && cd bgutil/server && npm ci && npx tsc && npm prune --omit=dev

# --- Stage 2: the app ---
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Node runtime: runs the PO-token server and solves YouTube's JS challenges for yt-dlp.
COPY --from=potserver /usr/local/bin/node /usr/local/bin/node
COPY --from=potserver /opt/bgutil/server /opt/bgutil-server

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app.py start.sh ./
COPY static ./static
RUN chmod +x start.sh

ENV PORT=10000
EXPOSE 10000
CMD ["./start.sh"]
