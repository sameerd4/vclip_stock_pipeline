FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY bin/docker-entrypoint /usr/local/bin/vclip-entrypoint
RUN chmod +x /usr/local/bin/vclip-entrypoint \
    && pip install --no-cache-dir ".[visual]"

ENTRYPOINT ["vclip-entrypoint"]
CMD ["--help"]
