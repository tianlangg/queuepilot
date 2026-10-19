FROM python:3.13-slim-bookworm
RUN pip install --no-cache-dir uv==0.13.1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-cache
COPY alembic.ini ./
COPY migrations ./migrations
RUN useradd --create-home --uid 10001 queuepilot
USER queuepilot
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "queuepilot.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
