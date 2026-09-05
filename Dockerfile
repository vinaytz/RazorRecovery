# RazorRecovery -- two stages, so the runtime image carries no compiler.
#
# The build is deliberately boring. No wheels are built at runtime, nothing is
# fetched at container start, and the image runs as a non-root user. Persistent
# state lives on /data: the sqlite database and results.json, both gitignored
# and both regenerated on first boot if the volume is empty.
#
# Python is pinned to 3.12 and that pin is load-bearing: `numpy==2.1.1` in
# requirements.txt publishes wheels for cp310-cp313 and none for 3.14, so on a
# host whose `python3` is 3.14 the plain `pip install -r requirements.txt` path
# fails while this image works. That is most of the reason this file exists.

# ---- stage 1: build the virtualenv -----------------------------------------
FROM python:3.12-slim AS builder

# numpy and scipy publish manylinux wheels for 3.12, so this needs no toolchain.
# If a future requirement does need one, add build-essential HERE and nowhere
# else -- that is the entire point of the split.
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

# ---- stage 2: the runtime image --------------------------------------------
FROM python:3.12-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RAZORRECOVERY_DB=/data/razorrecovery.db \
    DRY_RUN=true

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app/ app/
COPY sim/ sim/
COPY config/ config/
COPY web/ web/
COPY fixtures/ fixtures/
COPY tests/ tests/
COPY main.py run_benchmark.py requirements.txt ./
COPY README.md SPEC.md HANDOFF.md WHAT_WE_CUT.md ./
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Non-root. /app is owned by the app user because the entrypoint symlinks
# results.json into it; /data is a named volume in compose, so `docker compose
# down` without `-v` keeps the run history.
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
 && useradd --create-home --uid 10001 razor \
 && mkdir -p /data \
 && chown -R razor:razor /data /app

USER razor
EXPOSE 8000
VOLUME ["/data"]

# stdlib only -- adding curl to the image just to answer this would be a
# dependency bought for a healthcheck.
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=5 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
