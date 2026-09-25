"""Exercise the real OpenAI SDK over a local HTTP transport, never over the network."""
import json
import httpx
import pytest
from openai import OpenAI, RateLimitError
from conftest import synthetic_quote
from quoteiq import llm
from quoteiq.demo_data import generate_rfx
from quoteiq.models import ExtractedQuote


def test_gemini_400_exposes_safe_provider_reason():
    request = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/models/test:generateContent")
    response = httpx.Response(400, request=request, json={"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                                     "message": "Invalid JSON payload received."}})
    error = httpx.HTTPStatusError("bad request", request=request, response=response)
    message = llm.safe_error(error)
    assert "INVALID_ARGUMENT" in message
    assert "Invalid JSON payload" in message
    assert "Retry the preserved source" in message


def api_with_result(payload, captured, status="completed"):
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "resp_local_contract", "object": "response", "created_at": 1,
            "model": "gpt-4.1", "status": status, "error": None, "incomplete_details": None,
            "output": [{"type": "message", "id": "msg_contract", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": payload, "annotations": []}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        })
    return OpenAI(api_key="local-contract-test-placeholder", http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_real_sdk_structured_multimodal_request_and_parse(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai")
    rfx = generate_rfx()
    quote = synthetic_quote(rfx)
    requests = []
    api = api_with_result(quote.model_dump_json(), requests)
    monkeypatch.setattr(llm, "client", lambda: api)
    content = [{"type": "input_text", "text": "Fixture quote"},
               {"type": "input_image", "image_url": "data:image/png;base64,fixture", "detail": "high"}]
    result, metadata = llm.extract_quote(rfx, content)
    assert isinstance(result, ExtractedQuote)
    assert len(result.lines) == 30 and metadata["response_id"] == "resp_local_contract"
    sent = requests[0]
    assert sent["store"] is False
    assert sent["text"]["format"]["strict"] is True
    assert sent["text"]["format"]["schema"]["additionalProperties"] is False
    assert sent["input"][0]["content"][1]["type"] == "input_image"
    assert "untrusted" in sent["instructions"]
    api.close()


def test_invalid_structured_response_is_not_accepted(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai")
    api = api_with_result('{"made_up_answer": 123}', [])
    monkeypatch.setattr(llm, "client", lambda: api)
    with pytest.raises(ValueError):
        llm.extract_quote(generate_rfx(), [{"type": "input_text", "text": "No facts"}])
    api.close()


@pytest.mark.parametrize("error_code", ["credit_balance_exhausted", "insufficient_quota", "billing_hard_limit_reached"])
def test_quota_error_is_actionable_without_exposing_provider_message(error_code):
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    error = RateLimitError("Provider diagnostics must not be echoed", response=response, body={"code": error_code})
    message = llm.safe_error(error)
    assert "credits or quota are exhausted" in message
    assert "billing" in message
    assert "Provider diagnostics" not in message
