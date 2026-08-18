FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    p7zip-full \
    unar \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && (pip install "cryptg>=0.5,<1" || echo "cryptg optional; continuing without it")

COPY app ./app
COPY tests ./tests
COPY pytest.ini ./pytest.ini
COPY .env.example ./.env.example

RUN python -m compileall -q app tests

CMD ["python", "-m", "app"]
