"""The local AI models: open-source models served by Ollama on this server (``ollama`` service).

No API key and nothing leaves the server. Two jobs, two sizes:
- the news brief, every few hours: ``AITRADER_LOCAL_MODEL`` (default Qwen2.5 1.5B), quick;
- reading books into strategy rules, overnight (``learn.bookqueue``): ``AITRADER_BOOK_MODEL``
  (default Qwen2.5 3B), slower and better at the strict rule language.
Ollama keeps one model loaded at a time and swaps them. Each answer is forced into a JSON schema
by Ollama (structured output) and then checked field by field by the caller, like any untrusted text.

``AITRADER_LLM_URL`` is the Ollama address (default ``http://ollama:11434``). The model is pulled
the first time it is needed, which downloads about a gigabyte once.
"""

from __future__ import annotations

import json
import os
import urllib.request

DEFAULT_URL = "http://ollama:11434"
DEFAULT_MODEL = "qwen2.5:1.5b"
DEFAULT_BOOK_MODEL = "qwen2.5:3b"
CONTEXT_TOKENS = 8192  # the feeds plus the answer; more costs memory the server doesn't have
BOOK_CONTEXT_TOKENS = 6144  # the rule language (~2k), a book part (~2.2k) and the answer, for the 3B model


def url() -> str:
    return (os.environ.get("AITRADER_LLM_URL") or DEFAULT_URL).rstrip("/")


def model_name() -> str:
    return os.environ.get("AITRADER_LOCAL_MODEL") or DEFAULT_MODEL


def book_model_name() -> str:
    return os.environ.get("AITRADER_BOOK_MODEL") or DEFAULT_BOOK_MODEL


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


def unload(model: str) -> None:
    """Give the model's memory back now, instead of after OLLAMA_KEEP_ALIVE (best effort)."""
    try:
        _post("/api/generate", {"model": model, "keep_alive": 0}, 60)
    except Exception:
        pass


def chat_json(system: str, user: str, schema: dict, model: str | None = None, timeout: float = 1200) -> dict:
    """One answer from the local model, shaped by ``schema``. Raises when it isn't valid JSON."""
    return chat_messages(system, [{"role": "user", "content": user}], schema, model or model_name(), timeout)


def chat_messages(system: str, messages: list[dict], schema: dict, model: str, timeout: float = 1200,
                  context: int = CONTEXT_TOKENS) -> dict:
    """An answer to a conversation (user and assistant turns, text only), shaped by ``schema``."""
    ensure_model(model)
    data = _post("/api/chat", {
        "model": model, "stream": False, "format": schema,
        "messages": [{"role": "system", "content": system},
                     *({"role": m["role"], "content": str(m["content"])} for m in messages)],
        "options": {"temperature": 0.2, "num_ctx": context},
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


def book_caller(schema: dict, timeout: float = 3600):
    """``call(system, messages) -> dict`` for ``learn.extract``: the book model, its smaller context.
    A part of a book can take many minutes on one CPU core, hence the long timeout."""
    def call(system: str, messages: list[dict]) -> dict:
        return chat_messages(system, messages, schema, book_model_name(), timeout, BOOK_CONTEXT_TOKENS)

    return call
