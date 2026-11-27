# Base images are pinned by multi-arch digest. Refresh with: docker buildx imagetools inspect python:3.12-slim
# Static profiles set DAYFRAME_BUNDLED_SITE=none: their website service supplies the default site.
ARG DAYFRAME_BUNDLED_SITE=starter

FROM node:24-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS site-starter
ENV ASTRO_TELEMETRY_DISABLED=1
WORKDIR /website
COPY website/package*.json ./
RUN npm ci
COPY website ./
RUN npm run build

FROM python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f AS site-none
RUN mkdir -p /website/dist

FROM site-${DAYFRAME_BUNDLED_SITE} AS site

FROM python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DAYFRAME_DATA_DIR=/data
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
RUN mkdir -p /run/dayframe-renderer /run/dayframe-site && chown 10001:10001 /run/dayframe-renderer /run/dayframe-site && chmod 770 /run/dayframe-renderer /run/dayframe-site
# An empty named volume takes the owner of the image's folder, so Art Frames can write /data with no setup step.
# A bind mount (./data:/data in compose.base.yaml) hides this folder and keeps the host folder's owner.
RUN mkdir -p /data/layout-settings/starter /data/layout-state/starter && chown -R 10001:10001 /data && chmod 700 /data
COPY art_frames /app/art_frames
COPY --from=site /website/dist /app/art_frames/site
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+(os.getenv('ART_FRAMES_PORT') or os.getenv('DAYFRAME_PORT') or '8765')+'/health',timeout=3)"
# The rollback reads this label: this image can read settings with the week schedule.
LABEL art-frames.settings=week
# The release workflow sets VERSION from the git tag. A local build says "dev".
ARG VERSION=dev
LABEL org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.source=https://github.com/brianmuse/art-frames \
      org.opencontainers.image.title="Art Frames"
CMD ["python", "-m", "art_frames"]
