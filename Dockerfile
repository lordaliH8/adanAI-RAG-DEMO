FROM python:3.12-slim@sha256:5b8b1421ccb6a525860a77863c049eab5847076cda34e030c75a80c58faf3f3c

COPY --from=ghcr.io/astral-sh/uv:0.12.2@sha256:069a51314a7bb6031777a9273205fe1b0b19e914ef418207d1338b268df641dd /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    TIKTOKEN_CACHE_DIR=/app/tokenizer_cache \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

# The application image contains business inputs only, with no authoring sources,
# evaluation answers, reports, credentials, Git history, or local caches.
COPY app/ ./app/
COPY ui/ ./ui/
COPY data/ ./data/
COPY .streamlit/config.toml ./.streamlit/config.toml
RUN mkdir -p /app/var

EXPOSE 8000 8501
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
