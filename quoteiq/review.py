"""Validated buyer actions, shared by UI and integration tests."""
from collections import Counter
from .models import ExtractedLine, ExtractedQuote, LineReview, TermsReview


def quote_lines(repo, doc_id, quote=None):
    quote = quote or ExtractedQuote.model_validate_json(repo.document(doc_id)["extraction"])
    return list(quote.lines) + [ExtractedLine.model_validate(value) for value in repo.manual_lines(doc_id)]


def add_manual_quote_line(repo, doc_id, line, review, actor):
    extracted = ExtractedLine.model_validate(line)
    checked = LineReview.model_validate(review)
    if checked.decision != "approved":
        raise ValueError("A manually added line must be reviewed and approved")
    if checked.item_id not in {item.id for item in repo.rfx().items}:
        raise ValueError("Choose a valid RFx item")
    return repo.add_manual_line(doc_id, extracted.model_dump(), checked.model_dump(), actor)


def approval_missing_fields(item_id, price, currency, price_basis, offered_quantity, spec_compliant):
    fields = []
    if not item_id:
        fields.append("RFx item match")
    if price is None:
        fields.append("price")
    if not currency:
        fields.append("currency")
    if price_basis == "unknown":
        fields.append("known unit")
    if offered_quantity is None:
        fields.append("quantity")
    return fields


def save_line_review(repo, doc_id, index, value, actor):
    review = LineReview.model_validate(value)
    if review.decision == "approved" and review.item_id not in {i.id for i in repo.rfx().items}:
        raise ValueError("Choose a valid RFx item")
    quote = ExtractedQuote.model_validate_json(repo.document(doc_id)["extraction"])
    if index < 0 or index >= len(quote_lines(repo, doc_id, quote)):
        raise ValueError("Unknown quote line")
    repo.save_review(doc_id, str(index), review.model_dump(), actor)


def save_terms_review(repo, doc_id, value, actor):
    review = TermsReview.model_validate(value)
    ids = [q.question_id for q in review.quality]
    expected = {q.id for q in repo.rfx().questionnaire}
    if len(ids) != len(set(ids)) or not set(ids).issubset(expected):
        raise ValueError("Quality answers must use unique RFx question IDs")
    repo.save_review(doc_id, "terms", review.model_dump(), actor)


def batch_candidates(repo, doc_id):
    """Only exact, unique, high-confidence, complete lines can enter a buyer-confirmed batch."""
    quote = ExtractedQuote.model_validate_json(repo.document(doc_id)["extraction"])
    reviewed = repo.reviews(doc_id)
    counts = Counter(x.matched_item_id for x in quote.lines)
    ids = {i.id for i in repo.rfx().items}
    results = []
    for idx, line in enumerate(quote.lines):
        if str(idx) in reviewed or line.issues or line.confidence < .85:
            continue
        if line.raw_item.strip().upper() != line.matched_item_id or counts[line.matched_item_id] != 1:
            continue
        if line.matched_item_id not in ids or line.spec_compliant is None or not line.evidence.locator or not line.evidence.excerpt:
            continue
        try:
            value = LineReview(item_id=line.matched_item_id, price=line.price, currency=line.currency or quote.terms.currency,
                               price_basis=line.price_basis, offered_quantity=line.offered_quantity, spec_compliant=line.spec_compliant,
                               decision="approved", reason="Candidate only; buyer reason required on save")
            results.append((idx, value))
        except ValueError:
            continue
    return results


def approve_verified_batch(repo, doc_id, reason, actor, confirmed):
    if not confirmed:
        raise ValueError("Confirm that you checked every candidate against the original source")
    if len(reason.strip()) < 3 or not actor.strip():
        raise ValueError("Reviewer name and a review reason are required")
    candidates = batch_candidates(repo, doc_id)
    if not candidates:
        raise ValueError("No complete unreviewed lines qualify; use individual review")
    for index, review in candidates:
        review.reason = reason
        save_line_review(repo, doc_id, index, review.model_dump(), actor)
    return len(candidates)


def guided_demo_review(repo, actor, reason, confirmed):
    """Buyer-confirmed review of complete extracted demo values; missing facts remain exceptions."""
    if not confirmed:
        raise ValueError("Confirm that you checked the proposed values against the demo originals")
    if len(reason.strip()) < 3:
        raise ValueError("Enter a guided review reason of at least 3 characters")
    valid_items = {item.id for item in repo.rfx().items}
    prepared_lines, prepared_terms = [], []
    exception_rows = []
    for doc in repo.documents(active_only=True):
        if not doc["extraction"] or doc["rfx_hash"] != repo.rfx_hash():
            continue
        quote = ExtractedQuote.model_validate_json(doc["extraction"])
        counts = Counter(line.matched_item_id for line in quote.lines if line.matched_item_id)
        existing = repo.reviews(doc["id"])
        for index, line in enumerate(quote.lines):
            if str(index) in existing:
                continue
            missing = approval_missing_fields(line.matched_item_id, line.price, line.currency or quote.terms.currency,
                                               line.price_basis, line.offered_quantity, line.spec_compliant)
            if line.matched_item_id not in valid_items:
                missing.append("valid RFx item match")
            if line.matched_item_id and counts[line.matched_item_id] != 1:
                missing.append("unique RFx item match")
            if line.spec_compliant is None:
                missing.append("technical verdict")
            if not line.evidence.locator or not line.evidence.excerpt:
                missing.append("source evidence")
            if missing:
                exception_rows.append({"supplier_id": doc["supplier_id"], "filename": doc["filename"],
                                       "line": index + 1, "item_id": line.matched_item_id,
                                       "needs_review": ", ".join(dict.fromkeys(missing))})
                continue
            prepared_lines.append((doc["id"], index, LineReview(
                item_id=line.matched_item_id, price=line.price, currency=line.currency or quote.terms.currency,
                price_basis=line.price_basis, offered_quantity=line.offered_quantity,
                spec_compliant=line.spec_compliant, decision="approved", reason=reason.strip())))
        terms = quote.terms
        quality_ids = {answer.question_id for answer in quote.quality}
        required_quality = {question.id for question in repo.rfx().questionnaire}
        commercial_complete = (terms.currency and terms.freight is not None and terms.tax_included is not None
                               and terms.lead_days is not None and bool(terms.payment_terms.strip())
                               and required_quality.issubset(quality_ids))
        if "terms" not in existing and commercial_complete:
            prepared_terms.append((doc["id"], TermsReview(
                terms=terms, quality=quote.quality, discount_decision="auto",
                decision="approved", reason=reason.strip())))
    for doc_id, index, review in prepared_lines:
        save_line_review(repo, doc_id, index, review.model_dump(), actor)
    for doc_id, review in prepared_terms:
        save_terms_review(repo, doc_id, review.model_dump(), actor)
    repo.log("guided_demo_review", repo.rfx_hash(), after={"approved_lines": len(prepared_lines),
             "approved_commercial_reviews": len(prepared_terms), "exceptions": exception_rows},
             reason=reason.strip(), actor=actor)
    return {"approved_lines": len(prepared_lines), "approved_terms": len(prepared_terms), "exceptions": exception_rows}


def review_sources_bulk(repo, doc_ids, decision, reason, actor, confirmed):
    """Apply one explicit buyer decision to all lines and terms in selected sources."""
    if decision not in {"approved", "rejected"}:
        raise ValueError("Choose approve or reject")
    if not doc_ids:
        raise ValueError("Select at least one source")
    if not confirmed:
        raise ValueError("Confirm that you reviewed every selected source")
    if len(reason.strip()) < 3:
        raise ValueError("Enter a bulk source review reason of at least 3 characters")
    prepared = []
    for doc_id in doc_ids:
        doc = repo.document(doc_id)
        if not doc or not doc["extraction"]:
            raise ValueError("Every selected source must have a completed extraction")
        if not doc["active"] or doc["rfx_hash"] != repo.rfx_hash():
            raise ValueError("Bulk review accepts only active sources for the current RFx")
        quote = ExtractedQuote.model_validate_json(doc["extraction"])
        lines = []
        for index, line in enumerate(quote.lines):
            missing = approval_missing_fields(
                line.matched_item_id, line.price, line.currency or quote.terms.currency,
                line.price_basis, line.offered_quantity, line.spec_compliant,
            )
            if decision == "approved" and missing:
                raise ValueError(f"{doc['filename']} line {index + 1} cannot be approved; missing {', '.join(missing)}. Review it individually or reject it.")
            lines.append((index, LineReview(
                item_id=line.matched_item_id, price=line.price,
                currency=line.currency or quote.terms.currency,
                price_basis=line.price_basis, offered_quantity=line.offered_quantity,
                spec_compliant=line.spec_compliant, decision=decision,
                reason=reason.strip(),
            )))
        terms = TermsReview(
            terms=quote.terms, quality=quote.quality, discount_decision="auto",
            decision=decision, reason=reason.strip(),
        )
        prepared.append((doc_id, lines, terms))
    # Constructing all models above validates the whole request before writes begin.
    for doc_id, lines, terms in prepared:
        for index, value in lines:
            save_line_review(repo, doc_id, index, value.model_dump(), actor)
        save_terms_review(repo, doc_id, terms.model_dump(), actor)
    return {"sources": len(prepared), "lines": sum(len(lines) for _, lines, _ in prepared)}
