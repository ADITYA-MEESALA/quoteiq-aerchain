"""Opt-in API contract test. Skipped by default; never passes using mocked answers."""
import os
import pytest
from quoteiq.demo_data import save_demo_files
from quoteiq.extractors import ingest
from quoteiq import llm

LIVE = pytest.mark.skipif(
    os.getenv("QUOTEIQ_RUN_LIVE") != "1" or not (os.getenv("OPENAI_API_KEY") or os.getenv("GEMINI_API_KEY")),
    reason="Requires a configured provider key and QUOTEIQ_RUN_LIVE=1; may incur API usage",
)


@pytest.mark.live
@LIVE
def test_live_rfx_generation_and_all_five_formats(repo, tmp_path):
    from quoteiq.demo_data import BRIEF
    rfx, metadata = llm.generate_rfx(BRIEF)
    assert len(rfx.items) == 30 and metadata["response_id"]
    repo.set_state("rfx", rfx.model_dump())
    paths = save_demo_files(tmp_path / "live_demo", rfx)
    for sid, path in paths.items():
        doc_id = ingest(repo, sid, path.name, path.read_bytes())
        doc = repo.document(doc_id)
        assert doc["status"] == "extracted", doc["error"]
        from quoteiq.models import ExtractedQuote
        quote = ExtractedQuote.model_validate_json(doc["extraction"])
        assert len(quote.lines) == (27 if sid == "S5" else 30)
        assert any(line.evidence.locator for line in quote.lines)
        if sid == "S3":
            assert quote.terms.currency == "USD"
        if sid == "S2":
            assert quote.terms.discount_kind == "other"
            assert "advance" in quote.terms.raw_discount.lower()


@pytest.mark.live
@LIVE
def test_live_analyst_uses_deterministic_evidence(repo):
    from conftest import store_quote
    from quoteiq.analyst import ask
    from quoteiq.calculations import build_snapshot

    store_quote(repo)
    result = ask("Which supplier totals are available, and what evidence supports the answer?", build_snapshot(repo), repo.rfx())
    assert result["answer"].strip()
    assert result["evidence"]
    assert all(item.get("operation") for item in result["evidence"])
