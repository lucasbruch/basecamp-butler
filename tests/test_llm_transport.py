"""What the LLM call does when Ollama answers badly.

The distinction under test is not academic. `_ask_ollama` has two failure
returns and they mean opposite things to the caller: `UNREACHABLE` says "try
this again later", `None` says "the model looked at it and gave nothing usable",
and on the auto-reply path the second one moves the watermark past the message.
A busy or half-loaded Ollama answering 500 used to land in the catch-all and
come back as `None` — so the ping was consumed and silently never answered.
"""
import httpx

from app.classifier import ollama


def _answer(status, payload=None):
    request = httpx.Request("POST", "http://ollama/api/generate")
    return httpx.Response(status, json=payload or {}, request=request)


def _replies_with(monkeypatch, response):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: response)


def test_a_struggling_host_is_unreachable_not_a_refusal(monkeypatch):
    """500 = the model is still loading, or the GPU is out of memory. Retryable."""
    _replies_with(monkeypatch, _answer(500))
    assert ollama._ask_ollama("hi", "system") is ollama.UNREACHABLE


def test_a_rate_limited_host_is_unreachable_too(monkeypatch):
    _replies_with(monkeypatch, _answer(429))
    assert ollama._ask_ollama("hi", "system") is ollama.UNREACHABLE


def test_a_rejected_request_is_not_retried_forever(monkeypatch):
    """A 404 (say, a model name that doesn't exist) won't fix itself, and
    retrying it every minute would only fill the log."""
    _replies_with(monkeypatch, _answer(404))
    assert ollama._ask_ollama("hi", "system") is None


def test_a_dead_socket_is_unreachable(monkeypatch):
    def refuse(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", refuse)
    assert ollama._ask_ollama("hi", "system") is ollama.UNREACHABLE


def test_an_answer_that_is_not_an_object_is_unusable(monkeypatch):
    """`format: json` asks for an object but doesn't guarantee one, and every
    caller goes straight to .get() on the result."""
    _replies_with(monkeypatch, _answer(200, {"response": "[1, 2]"}))
    assert ollama._ask_ollama("hi", "system") is None


def test_an_object_comes_back_as_itself(monkeypatch):
    _replies_with(monkeypatch, _answer(200, {"response": '{"todo": true}'}))
    assert ollama._ask_ollama("hi", "system") == {"todo": True}


def test_a_reply_prompt_tells_the_model_which_lines_to_judge():
    """The transcript puts already-handled context above a divider. Without
    saying so, a small model reads its own earlier reply sitting there as proof
    that the new message has already been answered — and declines every
    follow-up in a thread it has spoken in once."""
    prompt = ollama.compose_reply_prompt(
        owner="Sam", person="Ana", tone=None, instructions=None
    )
    assert "--- new messages ---" in prompt
    assert "judge only the messages below it" in prompt
