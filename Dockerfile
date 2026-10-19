FROM python:3.14-slim
RUN pip install uv && useradd --create-home app
WORKDIR /app
COPY . .
RUN uv sync --no-dev --frozen
USER app
CMD [".venv/bin/python", "bot.py", "--daemon"]
