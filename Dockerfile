# syntax=docker/dockerfile:1

# Use a recent Python so pandas>=2.3.* etc. funktionieren
FROM python:3.11-slim

# Avoid interactive tzdata and keep image small
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install OS deps only if needed; slim images usually suffice for wheel-only installs.
# Uncomment if your requirements ever need system libs:
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     build-essential \
#  && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage layer caching
COPY requirements.txt /app/requirements.txt

RUN apt-get update \
 && apt-get install -y --no-install-recommends sqlite3 \
 && rm -rf /var/lib/apt/lists/*
# Install Python deps (wheel-only to avoid long builds)

RUN python -m pip install -U pip setuptools wheel \
 && python -m pip install --only-binary=:all: -r /app/requirements.txt

# Copy your script into the image
COPY ing2db.py /app/ing2db.py
# Default command: keep container alive with a trivial loop
# A TTY will be attached by compose; we keep it alive regardless.
CMD ["sleep", "infinity"]
