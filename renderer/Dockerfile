FROM python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/opt/browsers HOME=/tmp
WORKDIR /renderer
COPY renderer/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && python -m playwright install --with-deps chromium \
    && mkdir -p /run/dayframe-renderer \
    && chown 10001:10001 /run/dayframe-renderer && chmod 770 /run/dayframe-renderer
COPY renderer/server.py ./server.py
COPY art_frames/ipc.py ./art_frames/ipc.py
USER 10001:10001
CMD ["python", "server.py"]
