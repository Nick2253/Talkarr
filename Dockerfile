# --- build: compile wheels (webrtcvad, a ffsubsync dependency, needs gcc) ---
FROM python:3.12-slim AS build

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# --- runtime ---
FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/* && rm -rf /wheels

WORKDIR /app
COPY talkarr ./talkarr
COPY wsgi.py .

# UID/GID match the seastone media stack (PUID/PGID) so files Talkarr rewrites
# keep the library's ownership. The data dir is created with that owner so a
# fresh named volume inherits it.
RUN mkdir -p /data /lab && chown 23611:21105 /data /lab
USER 23611:21105

ENV PYTHONUNBUFFERED=1
EXPOSE 9494

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --start-interval=2s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:9494/health', timeout=4).status != 200)"

CMD ["gunicorn", "wsgi:app", "-b", "0.0.0.0:9494", "-w", "1", "--threads", "4", "--timeout", "300"]
