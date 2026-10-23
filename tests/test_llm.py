"""The local AI model client (Ollama): pulls the model once, forces the answer into a schema."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from aitrader import llm


@pytest.fixture
def ollama(monkeypatch):
    """A fake Ollama on localhost: ``seen`` records (path, body); ``state`` drives its answers."""
    seen, state = [], {"models": [], "answer": '{"market_risk": "normal"}'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            seen.append((self.path, None))
            self.reply({"models": [{"name": m} for m in state["models"]]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append((self.path, body))
            if self.path == "/api/pull":
                state["models"].append(body["model"])
                self.reply({"status": "success"})
            else:
                self.reply({"message": {"role": "assistant", "content": state["answer"]}})

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("AITRADER_LLM_URL", f"http://127.0.0.1:{server.server_port}/")
    monkeypatch.delenv("AITRADER_LOCAL_MODEL", raising=False)
    yield seen, state
    server.shutdown()


def test_chat_pulls_the_model_once_and_sends_the_schema(ollama):
    seen, state = ollama
    schema = {"type": "object", "properties": {"market_risk": {"type": "string"}}}
    assert llm.chat_json("be careful", "the news", schema) == {"market_risk": "normal"}
    assert [p for p, _ in seen] == ["/api/tags", "/api/pull", "/api/chat"]
    assert seen[1][1] == {"model": llm.DEFAULT_MODEL, "stream": False}
    chat = seen[2][1]
    assert chat["format"] == schema and chat["stream"] is False and chat["model"] == llm.DEFAULT_MODEL
    assert chat["messages"] == [{"role": "system", "content": "be careful"}, {"role": "user", "content": "the news"}]
    seen.clear()
    llm.chatter()("s", "u", schema)  # already there: no second download
    assert [p for p, _ in seen] == ["/api/tags", "/api/chat"]


def test_chat_refuses_an_answer_that_is_not_a_json_object(ollama):
    _seen, state = ollama
    state["answer"] = "Sure! Here is the brief: risk is normal."
    with pytest.raises(RuntimeError, match="valid JSON"):
        llm.chat_json("s", "u", {})
    state["answer"] = "[1, 2]"
    with pytest.raises(RuntimeError, match="JSON object"):
        llm.chat_json("s", "u", {})
