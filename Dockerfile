FROM python:3.14-slim
# ripgrep backs search_vault / /search (vault_search.py shells out to `rg`).
# The `claude` CLI is deliberately NOT installed: /add-tool is
# unsupported in the container — set `allow_self_modification: false` in config.yaml.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/*
RUN pip install uv && useradd --create-home app
# Mount points for the persistent volumes declared in compose.yaml
# (XDG_DATA_HOME=/data, XDG_STATE_HOME=/state); owned by `app` so named
# volumes inherit writable ownership on first mount.
RUN mkdir -p /data /state /vault && chown app:app /data /state /vault
WORKDIR /app
COPY . .
RUN uv sync --no-dev --frozen
USER app
CMD [".venv/bin/python", "bot.py"]
