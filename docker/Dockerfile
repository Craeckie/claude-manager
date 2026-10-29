FROM python:3.12-slim

# sync_git_status shells out to `git status` against every project's bind-mounted
# checkout -- the slim base has no git binary at all.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies first (cached unless pyproject.toml / uv.lock changes)
COPY pyproject.toml uv.lock* ./
RUN uv sync --locked --no-dev

# Copy application code
COPY . .

RUN chmod +x /app/docker/run.sh

# Run as uid/gid 1000, not root: bind-mounted host directories (media,
# static_collected, LOG_ANALYSIS_ROOT) are owned by the host's uid 1000, and
# a root process denied CAP_DAC_OVERRIDE there fails with EACCES.
RUN groupadd -g 1000 app && useradd -u 1000 -g 1000 -m -d /home/app app \
    && mkdir -p /app/media /app/static_collected \
    && chown -R 1000:1000 /app /home/app
ENV HOME=/home/app
USER 1000:1000

CMD ["/app/docker/run.sh"]
