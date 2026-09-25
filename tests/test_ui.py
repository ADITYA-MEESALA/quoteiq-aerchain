import json
from pathlib import Path
from streamlit.testing.v1 import AppTest
from conftest import store_quote


def test_overview_shows_aerchain_workspace_badge(repo, monkeypatch):
    monkeypatch.setenv("QUOTEIQ_DATA_DIR", str(repo.root))
    app = AppTest.from_file(Path("streamlit_app.py").resolve(), default_timeout=30).run()

    assert_clean(app)
    assert any("Aerchain Builder" in element.value and "Sourcing workspace" in element.value
               for element in app.markdown)


def test_blocker_guidance_exposes_line_causes_and_routes_cost_dependencies():
    from quoteiq.ui import blocker_action, blocker_table, line_blocker_table

    snapshot = {
        "suppliers": [{"supplier_id": "S1", "supplier": "Supplier one",
                       "blockers": ["Line-level review or normalization issues remain", "Not all line costs are known"],
                       "eligibility_reasons": []}],
        "cheapest_lines": [{"item_id": "PKG-007"}, {"item_id": "PKG-008"}],
        "lines": [{"supplier_id": "S1", "supplier": "Supplier one", "line_index": 6,
                   "item_id": "PKG-007", "raw_item": "Seven", "source_locator": "Sheet1!A9",
                   "review_status": "pending", "line_total": None,
                   "issues": ["Human review required", "Missing price"]}],
    }
    rows = blocker_table(snapshot)
    assert "1× Human review required" in rows[0]["Details"]
    assert "1/1 extracted active line" in rows[1]["Details"]
    assert blocker_action("Not all line costs are known") == "Resolve the related line, FX, unit or terms blocker"
    assert blocker_action("Quotation validity is short") == "Review desk → Commercial & quality"
    exact = line_blocker_table(snapshot)
    assert exact[0]["Line"] == 7
    assert exact[0]["RFx item"] == "PKG-007"
    assert exact[1]["Issue"] == "Missing price"


def test_incomplete_blocker_names_missing_rfx_item():
    from quoteiq.ui import blocker_table

    snapshot = {
        "suppliers": [{"supplier_id": "S1", "supplier": "Supplier one", "blockers": ["Incomplete or duplicate quote: 1/2 RFx items"],
                       "eligibility_reasons": []}],
        "cheapest_lines": [{"item_id": "PKG-001"}, {"item_id": "PKG-002"}],
        "lines": [{"supplier_id": "S1", "review_status": "approved", "item_id": "PKG-001", "issues": [], "line_total": 10}],
    }
    assert "Missing after review: PKG-002" in blocker_table(snapshot)[0]["Details"]


def test_missing_quote_is_shown_as_awaiting_not_critical():
    from quoteiq.ui import blocker_table

    snapshot = {"suppliers": [{"supplier_id": "S1", "supplier": "Supplier one",
                                "blockers": ["No extracted quote"], "eligibility_reasons": []}],
                "lines": [], "cheapest_lines": []}
    assert blocker_table(snapshot)[0]["Type"] == "Awaiting quote"


def test_eligibility_guidance_requires_supplier_evidence():
    from quoteiq.ui import issue_guidance

    lead = issue_guidance("Lead time exceeds 21 days")
    quality = issue_guidance("Q2: quality requirement failed, unknown or unreviewed")
    assert "revised supplier response" in lead
    assert "keep this supplier ineligible" in lead
    assert "certificate" in quality
    assert "Unknown correctly keeps the supplier ineligible" in quality


def test_visible_headline_totals_cannot_include_hidden_supplier():
    from quoteiq.ui import lowest_visible

    visible = [
        {"supplier": "Deccan Corrupack", "total": 3418004.10, "eligible": True},
        {"supplier": "KraftNest Packaging", "total": 3556005.00, "eligible": True},
    ]
    result = lowest_visible(visible)
    assert result == {"suppliers": ["Deccan Corrupack"], "total": 3418004.10}
    assert "GreenFold Industries" not in result["suppliers"]


def assert_clean(app):
    assert not app.exception
    assert not app.error, [x.value for x in app.error]


def test_prepare_demo_and_navigate_all_screens(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTEIQ_DATA_DIR", str(tmp_path / "ui"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = AppTest.from_file(Path("streamlit_app.py").resolve(), default_timeout=30).run()
    assert_clean(app)
    app.button[0].click().run()
    assert_clean(app)
    assert app.metric[0].value == "30"
    for page in ["RFx studio", "Suppliers", "Quote inbox", "Review desk", "Compare & decide", "AI analyst", "Audit trail"]:
        app.sidebar.radio[0].set_value(page).run()
        assert_clean(app)
    app.sidebar.radio[0].set_value("Suppliers").run()
    button = next(b for b in app.button if b.label == "Simulate RFx invitations")
    button.click().run()
    assert_clean(app)
    assert any("5 simulated invitations" in x.value for x in app.success)


def test_review_and_comparison_with_persisted_data(repo, monkeypatch):
    store_quote(repo)
    monkeypatch.setenv("QUOTEIQ_DATA_DIR", str(repo.root))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = AppTest.from_file(Path("streamlit_app.py").resolve(), default_timeout=30).run()
    for page in ["Review desk", "Compare & decide", "Audit trail"]:
        app.sidebar.radio[0].set_value(page).run()
        assert_clean(app)


def test_compare_page_embeds_blocker_resolution_editor(repo, monkeypatch):
    store_quote(repo, approved=False)
    monkeypatch.setenv("QUOTEIQ_DATA_DIR", str(repo.root))
    app = AppTest.from_file(Path("streamlit_app.py").resolve(), default_timeout=30).run()
    app.sidebar.radio[0].set_value("Compare & decide").run()

    assert_clean(app)
    assert any(x.label == "Correction area" for x in app.radio)
    assert any(x.label == "Decision" for x in app.radio)
    assert any(x.label == "Save eligibility decision" for x in app.button)
    assert any(x.label == "Review / correction reason" for x in app.text_input)


def test_review_unknown_fields_and_batch_submission(repo, monkeypatch):
    from conftest import synthetic_quote
    quote = synthetic_quote(repo.rfx())
    quote.lines[0].price = None
    quote.lines[0].offered_quantity = None
    quote.lines[0].spec_compliant = None
    doc_id = store_quote(repo, quote, approved=False)
    monkeypatch.setenv("QUOTEIQ_DATA_DIR", str(repo.root))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = AppTest.from_file(Path("streamlit_app.py").resolve(), default_timeout=30).run()
    app.sidebar.radio[0].set_value("Review desk").run()
    assert_clean(app)
    next(x for x in app.checkbox if x.label == "I checked every listed candidate against the original source").check()
    next(x for x in app.text_input if x.label == "Batch review reason").set_value("Manually verified 29 complete fixture lines")
    next(x for x in app.button if x.label == "Approve verified candidate lines").click().run()
    assert_clean(app)
    assert len(repo.reviews(doc_id)) == 29
    assert "0" not in repo.reviews(doc_id)
    next(x for x in app.radio if x.label == "Review section").set_value("Commercial & quality").run()
    next(x for x in app.checkbox if x.label.startswith("I reviewed the source, all conditions")).check()
    next(x for x in app.button if x.label == "Approve reviewed terms and responses").click().run()
    assert any("Enter a commercial and quality review reason" in x.value for x in app.error)
    next(x for x in app.text_input if x.label == "Commercial and quality review reason").set_value("Verified test source terms")
    next(x for x in app.button if x.label == "Approve reviewed terms and responses").click().run()
    assert_clean(app)
    assert next(x for x in app.radio if x.label == "Review section").value == "Commercial & quality"
    assert any("Commercial terms and quality responses saved" in x.value for x in app.success)
    assert "terms" in repo.reviews(doc_id)


def test_bulk_reject_all_survives_form_submit_rerun(repo, monkeypatch):
    doc_id = store_quote(repo, approved=False)
    monkeypatch.setenv("QUOTEIQ_DATA_DIR", str(repo.root))
    app = AppTest.from_file(Path("streamlit_app.py").resolve(), default_timeout=30).run()
    app.sidebar.radio[0].set_value("Review desk").run()

    next(x for x in app.button if x.label == "Reject all").click().run()
    assert any("Draft updated: all 30 lines marked Rejected" in x.value for x in app.info)
    next(x for x in app.text_input if x.label == "Review / correction reason").set_value("Checked and rejected for test")
    next(x for x in app.button if x.label == "Save selected reviews").click().run()

    assert_clean(app)
    assert any("Saved 30 line reviews: 0 approved and 30 rejected" in x.value for x in app.success)
    reviews = repo.reviews(doc_id)
    assert len(reviews) == 30
    assert all(value["decision"] == "rejected" for value in reviews.values())


def test_approve_all_leaves_incomplete_lines_pending(repo, monkeypatch):
    from conftest import synthetic_quote

    quote = synthetic_quote(repo.rfx())
    quote.lines[0].price = None
    quote.lines[0].offered_quantity = None
    quote.lines[0].spec_compliant = None
    doc_id = store_quote(repo, quote, approved=False)
    monkeypatch.setenv("QUOTEIQ_DATA_DIR", str(repo.root))
    app = AppTest.from_file(Path("streamlit_app.py").resolve(), default_timeout=30).run()
    app.sidebar.radio[0].set_value("Review desk").run()

    next(x for x in app.button if x.label == "Approve all").click().run()
    assert any("1 incomplete lines remain Pending" in x.value for x in app.warning)
    next(x for x in app.text_input if x.label == "Review / correction reason").set_value("Verified complete source lines")
    next(x for x in app.button if x.label == "Save selected reviews").click().run()

    assert_clean(app)
    reviews = repo.reviews(doc_id)
    assert len(reviews) == 29
    assert "0" not in reviews
    assert all(value["decision"] == "approved" for value in reviews.values())


def test_reset_and_prepare_fresh_demo_clears_prior_work(repo, monkeypatch):
    doc_id = store_quote(repo)
    repo.set_state("demo_run_id", "old-run")
    monkeypatch.setenv("QUOTEIQ_DATA_DIR", str(repo.root))
    app = AppTest.from_file(Path("streamlit_app.py").resolve(), default_timeout=30).run()

    next(x for x in app.text_input if x.label == "Type RESET to confirm").set_value("RESET").run()
    next(x for x in app.button if x.label == "Reset and prepare fresh demo").click().run()

    assert_clean(app)
    assert repo.documents() == []
    assert repo.reviews(doc_id) == {}
    assert repo.get_state("demo_run_id") != "old-run"
    assert next(x for x in app.metric if x.label == "Quotes extracted").value == "0"
    assert any("Previous quotations, reviews, assumptions and audit history were deleted" in x.value for x in app.success)


def test_audit_values_decode_structures_and_keep_hashes_as_text():
    from quoteiq.ui import decode_audit_value

    digest = "4b6cdc480297311e7ad926028742b3a48111904514c13fb45609b2cc8e57a15e"
    assert decode_audit_value(json.dumps({"status": "approved"})) == {"status": "approved"}
    assert decode_audit_value(json.dumps(digest)) == digest
    assert decode_audit_value(digest) == digest
