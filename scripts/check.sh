#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts migrations
uv run pytest "$@"
