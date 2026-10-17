FROM python:3.12-slim

# a slow PyPI download is retried instead of failing the whole update
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10
WORKDIR /app

# The libraries first, in their own layer: Docker reuses it on every update that doesn't change
# pyproject.toml, so a code update downloads nothing from PyPI.
COPY pyproject.toml README.md ./
RUN python -c "import tomllib; print('\\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
    > /tmp/requirements.txt && pip install -r /tmp/requirements.txt setuptools && rm /tmp/requirements.txt
COPY src ./src
COPY docs ./docs
# The app runs as `trader`, with the same uid as the server user who owns ./data (AITRADER_UID,
# set by deploy/install.sh), so both can write there.
ARG UID=1000
RUN pip install --no-deps --no-build-isolation . && groupadd --gid "$UID" trader && useradd --create-home --uid "$UID" --gid "$UID" trader \
    && mkdir -p /app/data/books /app/data/kb && chown -R trader:trader /app/data

USER trader
ENTRYPOINT ["aitrader"]
CMD ["status"]
