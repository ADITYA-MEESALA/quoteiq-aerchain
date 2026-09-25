import pytest
from quoteiq.db import Repository
from quoteiq.demo_data import demo_suppliers, generate_rfx
from quoteiq.models import ExtractedQuote
from quoteiq.review import save_line_review, save_terms_review


@pytest.fixture
def repo(tmp_path):
    result = Repository(tmp_path / "workspace")
    result.set_state("rfx", generate_rfx().model_dump())
    result.set_state("suppliers", [s.model_dump() for s in demo_suppliers()])
    return result


def synthetic_quote(rfx, count=30, price=10, basis="piece", currency="INR"):
    """Unit-test fixture only; never imported by the application or demo pipeline."""
    evidence = {"locator": "test fixture row", "excerpt": "Explicit synthetic fixture, not a live AI result"}
    return ExtractedQuote.model_validate({
        "supplier_name": "Test supplier", "warnings": [],
        "lines": [{"raw_item": i.id, "raw_description": i.description, "raw_price": str(price), "raw_currency": currency,
                   "raw_unit": basis, "raw_quantity": str(i.quantity), "raw_spec": "RFx specifications accepted",
                   "matched_item_id": i.id, "price": price, "currency": currency, "price_basis": basis,
                   "offered_quantity": i.quantity, "spec_compliant": True, "confidence": .99, "issues": [], "evidence": evidence}
                  for i in rfx.items[:count]],
        "terms": {"currency": currency, "freight": 100, "raw_freight": "100", "discount_percent": 0,
                  "discount_kind": "none", "discount_threshold": None, "raw_discount": "No discount",
                  "lead_days": 14, "raw_lead_time": "14 days", "payment_terms": "Net 30",
                  "tax_included": False, "tax_percent": None, "raw_tax": "Excluded", "other_conditions": "", "evidence": [evidence]},
        "quality": [{"question_id": q.id, "raw_answer": "Yes", "meets_requirement": True, "evidence": evidence} for q in rfx.questionnaire]})


def store_quote(repo, quote=None, supplier="S1", approved=True, discount_decision="auto"):
    quote = quote or synthetic_quote(repo.rfx())
    doc_id = repo.add_document(supplier, "test-quote.txt", b"Unit-test quote evidence; not an OpenAI extraction")
    repo.save_parsed(doc_id, {"text": "Test source", "warnings": [], "image_locators": []})
    repo.finish_document(doc_id, quote.model_dump(), {"test_double": True})
    if approved:
        approve_quote(repo, doc_id, quote, discount_decision)
    return doc_id


def approve_quote(repo, doc_id, quote, discount_decision="auto"):
    for n, line in enumerate(quote.lines):
        save_line_review(repo, doc_id, n, {"item_id": line.matched_item_id, "price": line.price, "currency": line.currency,
                         "price_basis": line.price_basis, "offered_quantity": line.offered_quantity, "spec_compliant": line.spec_compliant,
                         "decision": "approved", "reason": "Verified synthetic test fixture"}, "Test reviewer")
    save_terms_review(repo, doc_id, {"terms": quote.terms.model_dump(), "quality": [q.model_dump() for q in quote.quality],
                      "discount_decision": discount_decision, "decision": "approved", "reason": "Verified synthetic fixture terms"}, "Test reviewer")
