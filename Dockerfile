# Cue without the terminal: `cue serve`, a web interface for phones and browsers that also
# casts to TVs. Run it with docker compose (see docker-compose.yml and the wiki's Web and
# Docker page): it needs the host's network, so it can find TVs and TVs can reach it.
FROM python:3.13-slim

# FFmpeg: for players and downloads that turn a stream into one file.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/cue
COPY pyproject.toml README.md LICENSE ./
COPY cue ./cue
RUN pip install . && rm -rf /opt/cue

# Everything Cue keeps (config.toml, its database, plugins, subtitles) lives in /config.
# HOME points there too, so any user ID works (docker compose's user: setting).
ENV CUE_HOME=/config \
    HOME=/config
RUN useradd --uid 1000 --home-dir /config --no-create-home --shell /usr/sbin/nologin cue \
    && mkdir /config && chown cue:cue /config
USER cue
WORKDIR /config
VOLUME /config

# 8771: the web interface. 8770: the cast server, which phones and TVs play videos from.
EXPOSE 8771 8770
STOPSIGNAL SIGTERM
CMD ["cue", "serve"]
