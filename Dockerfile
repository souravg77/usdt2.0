FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY docs ./docs
RUN pip install . && useradd --create-home --uid 1000 trader && mkdir -p /app/data/books /app/data/kb && chown -R trader /app/data

USER trader
ENTRYPOINT ["aitrader"]
CMD ["status"]
