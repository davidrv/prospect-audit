FROM python:3.13-slim

# WeasyPrint needs these native libs (Pango/cairo/gdk-pixbuf) — not installable via pip.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium for the Playwright fallback in official.py (JS-rendered store
# locators with no schema.org markup and nothing in the raw HTML either).
# --with-deps pulls in Chromium's native libs (fonts, libnss3, etc.) via apt —
# adds ~300-400MB to the image and ~30-90s to the build.
RUN playwright install --with-deps chromium

COPY . .

ENV PORT=8080
EXPOSE 8080

# --workers 1: the live-progress job store (app.py:_jobs) is an in-process
#   dict, so a job created in one worker is invisible to another — with >1
#   worker the frontend's /jobs/<id>/status polling hits the wrong worker
#   ~half the time and 404s. Audits are I/O-bound and already fan out across
#   threads internally, so a single worker with many threads serves concurrent
#   audits fine. (Scale past this later with a shared store: Redis/DB.)
# --timeout 180: a full multi-source audit + PDF render for a large chain can
#   take longer than gunicorn's 30s default worker timeout.
CMD exec gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 180 app:app
