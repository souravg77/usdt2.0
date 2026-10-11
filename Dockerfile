FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY docs ./docs
# The app runs as `trader`, with the same uid as the server user who owns ./data (AITRADER_UID,
# set by deploy/install.sh), so both can write there.
ARG UID=1000
RUN pip install . && groupadd --gid "$UID" trader && useradd --create-home --uid "$UID" --gid "$UID" trader \
    && mkdir -p /app/data/books /app/data/kb && chown -R trader:trader /app/data

USER trader
ENTRYPOINT ["aitrader"]
CMD ["status"]
