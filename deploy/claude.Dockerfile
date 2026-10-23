# Claude Code in its own container, for Remote Control from the Claude app (docs/change-requests.md).
# Built on the platform image, so it can run the tests with the same libraries.
#   docker compose --profile claude build claude
FROM aitrader:latest

USER root
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "pytest>=8" "pytest-asyncio>=0.23" "httpx>=0.27"

USER trader
# the native installer puts the claude command in ~/.local/bin
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH=/home/trader/.local/bin:$PATH \
    CLAUDE_CONFIG_DIR=/home/trader/.claude-config \
    PYTHONPATH=/work/src
WORKDIR /work
ENTRYPOINT []
CMD ["claude", "remote-control"]
