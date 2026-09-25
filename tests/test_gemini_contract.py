import json
from types import SimpleNamespace

import httpx

from conftest import store_quote, synthetic_quote
from quoteiq import llm
from quoteiq.analyst import ask
from quoteiq.calculations import build_snapshot
from quoteiq.demo_data import generate_rfx
from quoteiq.models import ExtractedQuote, RFx


def response_for(text):
    return httpx.Response(200, request=httpx.Request("POST", "https://generativelanguage.googleapis.com"), json={
        "responseId": "gemini-contract",
        "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20},
    })


def test_gemini_structured_sends_real_image_and_resolved_schema(monkeypatch):
    captured = []
    quote = synthetic_quote(generate_rfx())
    def post(self, url, **kwargs):
        captured.append((url, kwargs))
        return response_for(quote.model_dump_json())
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "local-test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    monkeypatch.setattr(httpx.Client, "post", post)
    result, metadata = llm.extract_quote(generate_rfx(), [
        {"type": "input_text", "text": "visible quote text"},
        {"type": "input_image", "image_url": "data:image/png;base64,QUJD", "detail": "high"},
    ])
    assert isinstance(result, ExtractedQuote)
    payload = captured[0][1]["json"]
    assert payload["contents"][0]["parts"][1]["inlineData"] == {"mimeType": "image/png", "data": "QUJD"}
    schema = payload["generationConfig"]["responseJsonSchema"]
    assert "$defs" not in json.dumps(schema) and "$ref" not in json.dumps(schema)
    assert '"const"' not in json.dumps(schema)
    rfx_schema = json.dumps(llm._inline_schema(RFx))
    assert '"const"' not in rfx_schema
    assert '"enum": ["piece"]' in rfx_schema
    assert metadata["provider"] == "gemini"
    assert "x-goog-api-key" in captured[0][1]["headers"]


def test_provider_selection_is_explicit_when_both_keys_exist(monkeypatch):
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini")
    try:
        llm.provider()
        assert False, "ambiguous credentials must not silently select a paid provider"
    except llm.AIError as exc:
        assert "Both API keys" in str(exc)
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    assert llm.provider() == "gemini"


def test_gemini_analyst_uses_validated_plan_and_deterministic_evidence(repo, monkeypatch):
    store_quote(repo)
    calls = []
    plan = '{"queries":[{"operation":"supplier_totals","supplier_ids":[],"item_ids":[],"eligible_only":false}]}'
    def post(self, url, **kwargs):
        calls.append(kwargs["json"])
        return response_for(plan if len(calls) == 1 else "KraftNest has the only complete reviewed total [E1].")
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "local-test-key")
    monkeypatch.setattr(httpx.Client, "post", post)
    result = ask("Which complete bid can I compare?", build_snapshot(repo), repo.rfx())
    assert result["evidence"][0]["operation"] == "supplier_totals"
    assert "[E1]" in result["answer"]
    assert "Validated deterministic tool evidence" in calls[1]["contents"][0]["parts"][0]["text"]


def test_gemini_structured_falls_back_from_schema_400(monkeypatch, repo):
    expected = synthetic_quote(repo.rfx())
    calls = []

    def fake_request(instructions, parts, schema=None, max_tokens=24000, json_mode=False):
        calls.append({"schema": schema, "json_mode": json_mode, "instructions": instructions})
        if schema is not None:
            raise llm.AIError("Gemini rejected this request (HTTP 400: INVALID_ARGUMENT).")
        return expected.model_dump_json(), {"totalTokenCount": 10}, "fallback-response"

    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    monkeypatch.setattr(llm, "_gemini_request", fake_request)
    result, metadata = llm._gemini_structured(ExtractedQuote, "Extract safely.", [{"type": "input_text", "text": "quote"}])

    assert len(result.lines) == 30
    assert metadata["structured_mode"] == "validated_json_fallback"
    assert calls[0]["schema"] is ExtractedQuote
    assert calls[1]["json_mode"] is True
    assert "Return exactly one JSON object" in calls[1]["instructions"]
