FROM python:3.11-slim AS build
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN pip wheel --no-cache-dir --wheel-dir /wheels ".[speed]"

FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --no-deps /wheels/*.whl && rm -rf /wheels
RUN python -c "import tgcrypto; import tg_media_archiver.cli; import tg_media_archiver.media_downloader"
LABEL org.opencontainers.image.title="tg-media-archiver" \
      org.opencontainers.image.source="https://github.com/CarmJos/tg-media-archiver" \
      org.opencontainers.image.licenses="MIT"
ENTRYPOINT ["tg-media-archiver"]
CMD ["run"]
