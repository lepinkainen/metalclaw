FROM python:3.14-slim
WORKDIR /app
COPY . .
RUN pip install uv && uv sync --no-dev
CMD ["uv", "run", "python", "telegram_bot.py"]
