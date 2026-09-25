from copy import deepcopy
from decimal import Decimal
import pytest
from conftest import approve_quote, store_quote, synthetic_quote
from quoteiq.calculations import build_snapshot, discount_amount
from quoteiq.models import Assumptions, LineReview
from quoteiq.normalization import fx_rate, money, normalize_line
from quoteiq.review import add_manual_quote_line, save_line_review, save_terms_review


def first(repo):
    return build_snapshot(repo)["suppliers"][0]


def test_complete_reviewed_bid_calculates_total(repo):
    store_quote(repo)
    expected = sum(i.quantity * 10 for i in repo.rfx().items) + 100
    assert first(repo)["total"] == expected
    assert first(repo)["eligible"] is True


def test_pending_review_never_becomes_eligible(repo):
    store_quote(repo, approved=False)
    snapshot = build_snapshot(repo)
    assert snapshot["suppliers"][0]["total"] is None
    assert not any(r["eligible"] for r in snapshot["lines"])
    assert "No eligible complete bid can be recommended yet" in snapshot["decision"]


def test_buyer_can_exclude_supplier_without_changing_extracted_quote(repo):
    store_quote(repo)
    before = build_snapshot(repo)
    assert before["suppliers"][0]["eligible"] is True
    repo.set_state("supplier_exclusions", {"S1": {"excluded": True, "reason": "Cannot meet revised delivery window"}})

    after = build_snapshot(repo)
    assert after["suppliers"][0]["eligible"] is False
    assert after["suppliers"][0]["manually_excluded"] is True
    assert "Buyer excluded supplier: Cannot meet revised delivery window" in after["suppliers"][0]["eligibility_reasons"]
    assert after["lowest_eligible"] is None
    assert after["lowest_total"] is None
    assert not any(warning.startswith("Test supplier:") for warning in after["critical_warnings"])
    assert not any(warning.startswith("Test supplier:") for warning in after["warnings"])
    assert after["lines"][0]["raw_price"] == before["lines"][0]["raw_price"]


def test_zero_critical_blockers_surfaces_lowest_eligible_decision(repo):
    store_quote(repo)
    snapshot = build_snapshot(repo)
    assert snapshot["critical_warnings"] == []
    assert "Recommended for buyer approval" in snapshot["decision"]
    assert "KraftNest Packaging" in snapshot["decision"]
    assert "buyer retains final approval authority" in snapshot["decision"]


def test_active_blockers_still_show_current_eligible_leader(repo):
    store_quote(repo)
    incomplete = synthetic_quote(repo.rfx(), count=27, price=9)
    store_quote(repo, incomplete, supplier="S2")
    snapshot = build_snapshot(repo)
    assert snapshot["critical_warnings"]
    assert "Current leading eligible bid: KraftNest Packaging" in snapshot["decision"]
    assert "Final recommendation remains provisional" in snapshot["decision"]


def test_unreceived_quotes_are_collection_status_not_critical_blockers(repo):
    snapshot = build_snapshot(repo)
    assert snapshot["extracted_supplier_count"] == 0
    assert len(snapshot["warnings"]) == len(repo.suppliers())
    assert snapshot["critical_warnings"] == []
    assert snapshot["decision"].startswith("Awaiting supplier quotations")


def test_unknown_fx_does_not_default(repo):
    quote = synthetic_quote(repo.rfx(), currency="USD")
    store_quote(repo, quote)
    assert first(repo)["total"] is None
    repo.set_state("assumptions", Assumptions(fx=[{"currency": "USD", "rate": 85, "as_of": "2026-09-23", "source": "Test-only buyer scenario"}]).model_dump())
    assert first(repo)["total"] == (sum(i.quantity * 10 for i in repo.rfx().items) + 100) * 85
    assert fx_rate("$", "INR", Assumptions())[0] is None


def test_per_100_and_box_conversion(repo):
    quote = synthetic_quote(repo.rfx(), price=1000, basis="100_pieces")
    store_quote(repo, quote)
    assert build_snapshot(repo)["lines"][0]["unit_price"] == 10
    quote = synthetic_quote(repo.rfx(), basis="box")
    store_quote(repo, quote)
    assert first(repo)["total"] is None
    repo.set_state("assumptions", Assumptions(box_is_piece=True, unit_reason="Source says individual box").model_dump())
    assert build_snapshot(repo)["lines"][0]["unit_price"] == 10


def test_custom_box_pack_size_conversion(repo):
    quote = synthetic_quote(repo.rfx(), price=500, basis="box")
    store_quote(repo, quote)
    repo.set_state("assumptions", Assumptions(requested_units_per_box=25, unit_reason="Supplier states 25 pieces per box").model_dump())
    line = build_snapshot(repo)["lines"][0]
    assert line["unit_price"] == 20
    assert "1 supplier box = 25" in line["conversion"]


def test_guided_demo_review_leaves_only_incomplete_rows(repo):
    from conftest import synthetic_quote
    from quoteiq.review import guided_demo_review

    quote = synthetic_quote(repo.rfx())
    for line in quote.lines[:3]:
        line.price = None
    doc_id = store_quote(repo, quote, approved=False)
    result = guided_demo_review(repo, "Demo buyer", "Checked against preserved demo source", True)

    assert result["approved_lines"] == 27
    assert result["approved_terms"] == 1
    assert len(result["exceptions"]) == 3
    assert len([key for key in repo.reviews(doc_id) if key != "terms"]) == 27


def test_partial_quote_never_competes_as_complete_but_can_win_lines(repo):
    store_quote(repo, synthetic_quote(repo.rfx(), count=27))
    snapshot = build_snapshot(repo)
    assert snapshot["suppliers"][0]["coverage"] == 27
    assert snapshot["suppliers"][0]["total"] is None
    assert snapshot["cheapest_lines"][0]["unit_price"] == 10
    assert snapshot["cheapest_lines"][29]["unit_price"] is None
    assert "Not all line costs are known" not in snapshot["suppliers"][0]["blockers"]


def test_conditional_discount_needs_explicit_decision(repo):
    quote = synthetic_quote(repo.rfx())
    quote.terms.discount_kind = "other"
    quote.terms.discount_percent = 3
    quote.terms.raw_discount = "Only for 20% advance and all items awarded"
    doc_id = store_quote(repo, quote)
    assert first(repo)["total"] is None
    approve_quote(repo, doc_id, quote, "ignore")
    undiscounted = first(repo)["total"]
    approve_quote(repo, doc_id, quote, "apply")
    assert first(repo)["total"] == money(Decimal(str(undiscounted - 100)) * Decimal(".97") + 100)


def test_threshold_uses_merchandise_not_freight(repo):
    quote = synthetic_quote(repo.rfx())
    terms = quote.terms
    terms.discount_kind, terms.discount_percent, terms.discount_threshold = "minimum_order", 5, 1000
    discount, _, blockers = discount_amount(terms, Decimal("999"), Decimal(1), "auto")
    assert discount == 0 and not blockers
    assert discount_amount(terms, Decimal("1000"), Decimal(1), "auto")[0] == 50


def test_partial_quote_cannot_evaluate_threshold(repo):
    quote = synthetic_quote(repo.rfx(), count=27)
    quote.terms.discount_kind, quote.terms.discount_percent, quote.terms.discount_threshold = "minimum_order", 5, 1000
    store_quote(repo, quote)
    assert first(repo)["discount"] is None
    assert any("incomplete" in x for x in first(repo)["blockers"])


@pytest.mark.parametrize("field", ["freight", "tax_included"])
def test_missing_commercial_facts_block_total(repo, field):
    quote = synthetic_quote(repo.rfx())
    setattr(quote.terms, field, None)
    store_quote(repo, quote)
    assert first(repo)["total"] is None


def test_included_tax_removed_from_goods_and_freight(repo):
    quote = synthetic_quote(repo.rfx(), price=11.8)
    quote.terms.tax_included, quote.terms.tax_percent, quote.terms.freight = True, 18, 118
    store_quote(repo, quote)
    assert first(repo)["total"] == sum(i.quantity * 10 for i in repo.rfx().items) + 100


def test_quality_failure_and_long_lead_exclude_but_keep_known_total(repo):
    quote = synthetic_quote(repo.rfx())
    quote.quality[0].meets_requirement = False
    quote.terms.lead_days = 45
    store_quote(repo, quote)
    snapshot = build_snapshot(repo)
    assert snapshot["suppliers"][0]["total"] is not None
    assert snapshot["suppliers"][0]["eligible"] is False
    assert snapshot["lowest_total"] and snapshot["lowest_eligible"] is None
    assert not any(x["eligible"] for x in snapshot["lines"])


def test_missing_quality_answer_is_not_invented(repo):
    quote = synthetic_quote(repo.rfx())
    quote.quality = []
    store_quote(repo, quote)
    assert first(repo)["eligible"] is False
    assert "Not provided" in build_snapshot(repo)["quality"][0]["raw_answer"]


def test_duplicate_lines_block_until_rejected(repo):
    quote = synthetic_quote(repo.rfx())
    quote.lines.append(quote.lines[0].model_copy(deep=True))
    doc_id = store_quote(repo, quote)
    assert first(repo)["total"] is None
    value = repo.reviews(doc_id)["30"]
    value.update(decision="rejected", reason="Duplicate of source row 0")
    save_line_review(repo, doc_id, 30, value, "Reviewer")
    assert first(repo)["total"] is not None


def test_rfx_revision_invalidates_reviewed_quote(repo):
    store_quote(repo)
    rfx = repo.rfx()
    rfx.items[0].quantity += 1
    repo.set_state("rfx", rfx.model_dump())
    assert first(repo)["total"] is None
    assert any("earlier RFx" in x for x in first(repo)["blockers"])


def test_human_correction_changes_calculation_preserves_original(repo):
    doc_id = store_quote(repo)
    original = first(repo)["total"]
    value = repo.reviews(doc_id)["0"]
    value.update(price=12, reason="Confirmed corrected rate with source")
    save_line_review(repo, doc_id, 0, value, "Ananya")
    assert first(repo)["total"] == original + 2 * repo.rfx().items[0].quantity
    assert '"price": 10.0' in repo.document(doc_id)["extraction"]
    audit = [a for a in repo.audit() if a["action"] == "human_review"]
    assert audit[0]["actor"] == "Ananya" and '12.0' in audit[0]["after_json"]


def test_cheapest_eligible_and_total_ties_preserved(repo):
    store_quote(repo, supplier="S1")
    store_quote(repo, supplier="S2")
    snapshot = build_snapshot(repo)
    assert len(snapshot["lowest_eligible"]["suppliers"]) == 2
    assert "KraftNest" in snapshot["cheapest_lines"][0]["suppliers"]
    assert "Deccan" in snapshot["cheapest_lines"][0]["suppliers"]


def test_insufficient_quantity_and_wrong_spec(repo):
    quote = synthetic_quote(repo.rfx())
    quote.lines[0].offered_quantity = 1
    quote.lines[1].spec_compliant = False
    store_quote(repo, quote)
    snapshot = build_snapshot(repo)
    assert snapshot["suppliers"][0]["total"] is None
    assert not snapshot["lines"][0]["eligible"] and not snapshot["lines"][1]["eligible"]


def test_no_substring_item_matching(repo):
    quote = synthetic_quote(repo.rfx())
    line = quote.lines[0]
    line.raw_item, line.matched_item_id = "box 1 maybe 10", None
    result = normalize_line(line, None, repo.rfx(), quote.terms, Assumptions())
    assert result["item_id"] is None
    assert "Unmatched RFx item" in result["issues"]


def test_money_uses_decimal_half_up():
    assert money(Decimal("1.005")) == 1.01
    assert money(Decimal("0.1") + Decimal("0.2")) == .3


def test_invalid_approvals_and_assumptions_rejected():
    with pytest.raises(ValueError):
        Assumptions(box_is_piece=True)
    with pytest.raises(ValueError):
        Assumptions(requested_units_per_box=25)
    with pytest.raises(ValueError):
        LineReview(item_id=None, price=None, currency=None, price_basis="unknown", offered_quantity=None,
                   spec_compliant=None, decision="approved", reason="Reviewed")
    rejected = LineReview(item_id=None, price=None, currency=None, price_basis="unknown", offered_quantity=None,
                          spec_compliant=None, decision="rejected", reason="Unrelated line")
    assert rejected.price is None
    verified_unknown = LineReview(item_id="PKG-001", price=10, currency="INR", price_basis="piece", offered_quantity=100,
                                  spec_compliant=None, decision="approved", reason="Source does not state compliance")
    assert verified_unknown.spec_compliant is None


def test_per_100_boxes_still_requires_box_assumption(repo):
    quote = synthetic_quote(repo.rfx(), basis="100_boxes", price=1000)
    store_quote(repo, quote)
    assert first(repo)["total"] is None
    repo.set_state("assumptions", Assumptions(box_is_piece=True, unit_reason="Buyer confirmed one box per requested unit").model_dump())
    assert build_snapshot(repo)["lines"][0]["unit_price"] == 10


def test_displayed_cost_components_reconcile(repo):
    quote = synthetic_quote(repo.rfx(), price=.01)
    quote.terms.freight = .005
    quote.terms.discount_kind, quote.terms.discount_percent = "unconditional", 1.237
    store_quote(repo, quote)
    s = first(repo)
    assert Decimal(str(s["total"])) == Decimal(str(s["subtotal"])) - Decimal(str(s["discount"])) + Decimal(str(s["freight"]))


def test_batch_review_requires_human_confirmation_and_skips_ambiguity(repo):
    from quoteiq.review import approve_verified_batch, batch_candidates
    quote = synthetic_quote(repo.rfx())
    quote.lines[0].issues = ["Illegible last digit"]
    quote.lines[1].confidence = .5
    doc_id = store_quote(repo, quote, approved=False)
    assert len(batch_candidates(repo, doc_id)) == 28
    with pytest.raises(ValueError):
        approve_verified_batch(repo, doc_id, "Verified source", "Buyer", False)
    assert not repo.reviews(doc_id)
    assert approve_verified_batch(repo, doc_id, "Verified all listed source lines", "Buyer", True) == 28
    assert "0" not in repo.reviews(doc_id) and "1" not in repo.reviews(doc_id)
    assert first(repo)["total"] is None


def test_bulk_source_rejection_reviews_lines_and_terms(repo):
    from conftest import store_quote
    from quoteiq.review import review_sources_bulk

    first = store_quote(repo, supplier="S1", approved=False)
    second = store_quote(repo, supplier="S2", approved=False)
    result = review_sources_bulk(repo, [first, second], "rejected", "Checked both originals", "Buyer", True)

    assert result == {"sources": 2, "lines": 60}
    for doc_id in (first, second):
        reviews = repo.reviews(doc_id)
        assert len(reviews) == 31
        assert all(value["decision"] == "rejected" for value in reviews.values())


def test_manual_line_completes_quote_without_changing_extraction(repo):
    quote = synthetic_quote(repo.rfx(), count=29)
    doc_id = store_quote(repo, quote)
    original = repo.document(doc_id)["extraction"]
    item = repo.rfx().items[29]
    line = {
        "raw_item": item.id, "raw_description": item.description, "raw_price": "12",
        "raw_currency": "INR", "raw_unit": "per piece", "raw_quantity": str(item.quantity),
        "raw_spec": "Confirmed compliant by supplier", "matched_item_id": item.id,
        "price": 12, "currency": "INR", "price_basis": "piece",
        "offered_quantity": item.quantity, "spec_compliant": True, "confidence": 1,
        "issues": [], "evidence": {"locator": "Supplier email paragraph 3", "excerpt": "PKG-030 INR 12 each"},
    }
    review = {
        "item_id": item.id, "price": 12, "currency": "INR", "price_basis": "piece",
        "offered_quantity": item.quantity, "spec_compliant": True, "decision": "approved",
        "reason": "Added from supplier clarification",
    }

    index = add_manual_quote_line(repo, doc_id, line, review, "Buyer")
    snapshot = build_snapshot(repo)

    assert index == 29
    assert repo.document(doc_id)["extraction"] == original
    assert len(repo.manual_lines(doc_id)) == 1
    assert snapshot["suppliers"][0]["coverage"] == 30
    assert snapshot["suppliers"][0]["eligible"] is True
    assert snapshot["lines"][-1]["source_locator"] == "Supplier email paragraph 3"
    assert any(event["action"] == "manual_line_added" for event in repo.audit())
