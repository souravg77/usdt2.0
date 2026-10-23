"""The local AI model: an open-source model served by Ollama on this server (``ollama`` service).

No API key and nothing leaves the server. The model is small enough for a few gigabytes of memory
and one CPU core (``AITRADER_LOCAL_MODEL``, default Qwen2.5 1.5B), so it is used only for work that
fits it: summarising the news feeds into the news brief. Its answer is forced into a JSON schema by
Ollama (structured output) and then checked field by field by the caller, like any untrusted text.

``AITRADER_LLM_URL`` is the Ollama address (default ``http://ollama:11434``). The model is pulled
the first time it is needed, which downloads about a gigabyte once.
"""

from __future__ import annotations

import json
import os
import urllib.request

DEFAULT_URL = "http://ollama:11434"
DEFAULT_MODEL = "qwen2.5:1.5b"
CONTEXT_TOKENS = 8192  # the feeds plus the answer; more costs memory the server doesn't have


def url() -> str:
    return (os.environ.get("AITRADER_LLM_URL") or DEFAULT_URL).rstrip("/")


def model_name() -> str:
    return os.environ.get("AITRADER_LOCAL_MODEL") or DEFAULT_MODEL


def _post(path: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url() + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def ensure_model(model: str, timeout: float = 1800) -> None:
    """Download the model into Ollama if it isn't there yet (once, about a gigabyte)."""
    with urllib.request.urlopen(url() + "/api/tags", timeout=30) as resp:
        have = {m.get("name") for m in json.loads(resp.read()).get("models", [])}
    if model not in have and f"{model}:latest" not in have:
        _post("/api/pull", {"model": model, "stream": False}, timeout)


def chat_json(system: str, user: str, schema: dict, model: str | None = None, timeout: float = 1200) -> dict:
    """One answer from the local model, shaped by ``schema``. Raises when it isn't valid JSON."""
    model = model or model_name()
    ensure_model(model)
    data = _post("/api/chat", {
        "model": model, "stream": False, "format": schema,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": 0.2, "num_ctx": CONTEXT_TOKENS},
    }, timeout)
    text = (data.get("message") or {}).get("content") or ""
    try:
        out = json.loads(text)
    except ValueError:
        raise RuntimeError(f"the local model {model} didn't return valid JSON") from None
    if not isinstance(out, dict):
        raise RuntimeError(f"the local model {model} didn't return a JSON object")
    return out


def chatter():
    """``chat(system, user, schema) -> dict``, the function the news brief calls."""
    def chat(system: str, user: str, schema: dict) -> dict:
        return chat_json(system, user, schema)

    return chat
