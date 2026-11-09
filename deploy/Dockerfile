# Ultron's image. Build and run it with deploy/docker-compose.yml (see README § Deploy).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/London

# zoneinfo needs the system timezone database
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Same uid as the server's `ubuntu` user, so files in the mounted data/ stay editable there
RUN useradd --uid 1000 --create-home ultron

WORKDIR /app
COPY pyproject.toml ./
COPY ultron ./ultron
RUN pip install .

USER ultron
CMD ["python", "-m", "ultron"]
