# Well Control Simulator -- classroom server.
#
# Built for a home Ubuntu server sitting behind Nginx Proxy Manager, which
# terminates TLS and forwards to the port published below. Nothing here
# terminates TLS itself.
#
#     docker compose up -d --build
#
# **One worker, and that is not a tuning choice.** Classes, rosters and every
# student's engine live in this process's memory, and the clock is a single
# asyncio task stepping them. A second worker would get its own empty set of
# classrooms and half the room would land in a class that does not exist.
# Scaling past one process means sharding classes across processes with a
# shared store, which is a different design, not a flag.

FROM python:3.13-slim

# Keeps the image small and the logs unbuffered so `docker logs` shows the
# generated instructor password at first boot.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# An editable-style install needs the source present, so this layer rebuilds on
# any source change. Fine here -- the dependency set is four packages and the
# install is seconds. Split it with a requirements file if that stops being true.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[sim]"

# Recordings and any future SQLite file land here; compose mounts a volume.
RUN mkdir -p /data && useradd --create-home --uid 10001 wellcontrol \
    && chown -R wellcontrol /data /app
USER wellcontrol

ENV WELLCONTROL_DATA_DIR=/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/speeds').read()" || exit 1

CMD ["uvicorn", "wellcontrol.sim.api:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
