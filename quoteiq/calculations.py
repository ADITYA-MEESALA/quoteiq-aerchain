"""Auditable cost engine. Decimal arithmetic; no LLM-generated calculations."""
import hashlib
import json
from collections import Counter
from .db import dumps
from .models import ExtractedQuote, TermsReview
from .normalization import dec, fx_rate, money, normalize_line, tax_factor
from .review import quote_lines


def discount_amount(terms, subtotal, rate, decision):
    if decision == "ignore":
        return dec(0), "Buyer explicitly excluded discount", []
    pct = terms.discount_percent
    if decision == "apply":
        if pct is None:
            return None, "Unknown discount percentage", ["Missing discount percentage"]
        return subtotal * dec(pct) / 100, "Buyer confirmed discount condition is satisfied", []
    if terms.discount_kind == "none":
        if pct not in (0, None):
            return None, "Conflicting discount terms", ["Discount kind and percentage conflict"]
        return dec(0), "Explicitly no discount", []
    if pct is None:
        return None, "Unknown discount", ["Missing discount terms"]
    if terms.discount_kind == "unconditional":
        return subtotal * dec(pct) / 100, f"Unconditional {pct}% of merchandise", []
    if terms.discount_kind == "minimum_order" and terms.discount_threshold is not None and rate is not None:
        threshold = dec(terms.discount_threshold) * rate
        qualifies = subtotal >= threshold
        return (subtotal * dec(pct) / 100 if qualifies else dec(0)), f"Merchandise {subtotal} >= threshold {threshold}: {qualifies}", []
    return None, terms.raw_discount or "Unknown condition", ["Unresolved discount condition"]


def build_snapshot(repo, audit=True):
    rfx = repo.rfx()
    if rfx is None:
        return {"lines": [], "suppliers": [], "quality": [], "cheapest_lines": [], "warnings": [], "decision": "Create an RFx first"}
    assumptions = repo.assumptions()
    supplier_exclusions = repo.get_state("supplier_exclusions", {})
    docs = {d["supplier_id"]: d for d in repo.documents(active_only=True)}
    rows, summaries, quality_table = [], [], []
    fingerprint_data = {"rfx": rfx.model_dump(), "assumptions": assumptions.model_dump(), "supplier_exclusions": supplier_exclusions,
                        "suppliers": [s.model_dump() for s in repo.suppliers()], "documents": []}
    for supplier in repo.suppliers():
        doc = docs.get(supplier.id)
        exclusion = supplier_exclusions.get(supplier.id, {})
        manually_excluded = bool(exclusion.get("excluded"))
        summary = {"supplier_id": supplier.id, "supplier": supplier.name, "coverage": 0, "reviewed": 0,
                   "subtotal": None, "discount": None, "freight": None, "total": None,
                   "eligible": False, "blockers": [], "eligibility_reasons": [], "discount_evidence": "",
                   "lead_days": None, "payment_terms": "", "source": None, "quality_pass": False,
                   "manually_excluded": manually_excluded, "exclusion_reason": exclusion.get("reason", "")}
        if not doc or not doc["extraction"]:
            summary["blockers"] = ["No extracted quote"]
            if manually_excluded:
                summary["eligibility_reasons"] = ["Buyer excluded supplier: " + exclusion.get("reason", "No reason recorded")]
            summaries.append(summary)
            continue
        reviews = repo.reviews(doc["id"])
        fingerprint_data["documents"].append({"id": doc["id"], "sha": doc["sha256"], "reviews": reviews, "extraction": doc["extraction"]})
        quote = ExtractedQuote.model_validate_json(doc["extraction"])
        tr = TermsReview.model_validate(reviews["terms"]) if "terms" in reviews else None
        terms = tr.terms if tr else quote.terms
        quality = tr.quality if tr else quote.quality
        term_blockers = []
        if not tr or tr.decision != "approved":
            term_blockers.append("Commercial terms and quality require approval")
        if doc["rfx_hash"] != repo.rfx_hash():
            term_blockers.append("Quote belongs to an earlier RFx revision")
        if quote.warnings and not tr:
            term_blockers.extend(quote.warnings)
        tax = tax_factor(terms)
        rate, fx_note = fx_rate(terms.currency, rfx.base_currency, assumptions)
        if rate is None:
            term_blockers.append(fx_note)
        if tax is None:
            term_blockers.append("Unknown tax treatment")
        if terms.freight is None:
            term_blockers.append("Missing freight")
        eligibility = []
        if terms.lead_days is None:
            eligibility.append("Missing lead time")
        elif terms.lead_days > rfx.max_lead_days:
            eligibility.append(f"Lead time exceeds {rfx.max_lead_days} days")
        if manually_excluded:
            eligibility.append("Buyer excluded supplier: " + exclusion.get("reason", "No reason recorded"))
        if not terms.payment_terms.strip():
            term_blockers.append("Missing payment terms")
        answers = {}
        for answer in quality:
            answers.setdefault(answer.question_id, []).append(answer)
        for question in rfx.questionnaire:
            found = answers.get(question.id, [])
            answer = found[0] if len(found) == 1 else None
            passes = bool(answer and answer.meets_requirement is True and tr and tr.decision == "approved")
            if question.mandatory and not passes:
                eligibility.append(f"{question.id}: quality requirement failed, unknown or unreviewed")
            quality_table.append({"supplier": supplier.name, "question_id": question.id, "question": question.question,
                                  "mandatory": question.mandatory, "raw_answer": answer.raw_answer if answer else "Not provided / duplicate",
                                  "meets_requirement": answer.meets_requirement if answer else None, "reviewed": bool(tr),
                                  "source": answer.evidence.locator if answer else "", "document_id": doc["id"]})
        normalized = []
        for idx, line in enumerate(quote_lines(repo, doc["id"], quote)):
            row = normalize_line(line, reviews.get(str(idx)), rfx, terms, assumptions)
            row.update(supplier=supplier.name, supplier_id=supplier.id, document_id=doc["id"], line_index=idx, filename=doc["filename"])
            normalized.append(row)
        counts = Counter(row["item_id"] for row in normalized if row["review_status"] != "rejected")
        for row in normalized:
            if row["review_status"] != "rejected" and counts[row["item_id"]] > 1:
                row["issues"].append("Duplicate quote lines for this RFx item")
        active_rows = [x for x in normalized if x["review_status"] != "rejected"]
        known = [x for x in active_rows if x["line_total"] is not None]
        subtotal = sum((dec(x["line_total"]) for x in known), dec(0))
        covered = {x["item_id"] for x in active_rows if x["item_id"] in {i.id for i in rfx.items}}
        full = len(covered) == len(rfx.items) and len(active_rows) == len(rfx.items)
        discount, discount_evidence, discount_issues = discount_amount(terms, subtotal, rate, tr.discount_decision if tr else "auto")
        # Minimum-order evaluation requires a complete order, never the partial known subtotal.
        if terms.discount_kind == "minimum_order" and not full and (not tr or tr.discount_decision == "auto"):
            discount, discount_issues = None, ["Threshold discount cannot be evaluated on an incomplete quote"]
        term_blockers.extend(discount_issues)
        freight = dec(terms.freight) * rate / tax if terms.freight is not None and rate is not None and tax else None
        blockers = list(term_blockers)
        if not full:
            blockers.append(f"Incomplete or duplicate quote: {len(covered)}/{len(rfx.items)} RFx items")
        has_line_issues = any(x["issues"] for x in active_rows)
        if has_line_issues:
            blockers.append("Line-level review or normalization issues remain")
        # Unknown costs are normally a consequence of an incomplete quote or a
        # line/terms blocker already shown above. Avoid counting that symptom as
        # another critical issue; retain it only as a defensive fallback.
        if len(known) != len(rfx.items) and full and not has_line_issues and not term_blockers:
            blockers.append("Not all line costs are known")
        # Round each displayed monetary component so the exported evidence reconciles exactly.
        discount = dec(money(discount)) if discount is not None else None
        freight = dec(money(freight)) if freight is not None else None
        total = money(subtotal - discount + freight) if not blockers and discount is not None and freight is not None else None
        for row in normalized:
            row["eligible"] = not row["issues"] and row["spec_compliant"] is True and not eligibility and not term_blockers
            row["eligibility_reasons"] = row["issues"] + eligibility + term_blockers + ([] if row["spec_compliant"] is True else ["Technical requirement not met"])
        if any(x["spec_compliant"] is not True for x in active_rows):
            eligibility.append("Technical requirements not met for every item")
        summary.update(coverage=len(covered), reviewed=sum(x["review_status"] == "approved" for x in active_rows),
                       subtotal=money(subtotal) if known else None, discount=money(discount) if discount is not None else None,
                       freight=money(freight) if freight is not None else None, total=total,
                       eligible=total is not None and not eligibility, blockers=list(dict.fromkeys(blockers)),
                       eligibility_reasons=list(dict.fromkeys(eligibility)), discount_evidence=discount_evidence,
                       lead_days=terms.lead_days, payment_terms=terms.payment_terms, source=doc["filename"],
                       quality_pass=not any("quality requirement" in x for x in eligibility),
                       raw_discount=terms.raw_discount, raw_freight=terms.raw_freight, raw_tax=terms.raw_tax,
                       other_conditions=terms.other_conditions, terms_evidence=[x.model_dump() for x in terms.evidence],
                       formula="Sum rounded extended line costs − merchandise discount + whole-order freight; pre-tax")
        rows.extend(normalized)
        summaries.append(summary)
    cheapest = []
    for item in rfx.items:
        candidates = [x for x in rows if x["item_id"] == item.id and x["eligible"] and x["unit_price_exact"] is not None]
        if candidates:
            best = min(dec(x["unit_price_exact"]) for x in candidates)
            winners = [x for x in candidates if dec(x["unit_price_exact"]) == best]
            cheapest.append({"item_id": item.id, "description": item.description,
                             "suppliers": ", ".join(x["supplier"] for x in winners), "unit_price": float(best),
                             "quantity": item.quantity, "material_total": money(best * item.quantity),
                             "sources": "; ".join(f"{x['filename']} | {x['source_locator']}" for x in winners)})
        else:
            cheapest.append({"item_id": item.id, "description": item.description, "suppliers": "No eligible reviewed bid",
                             "unit_price": None, "quantity": item.quantity, "material_total": None, "sources": ""})
    # An invited supplier that has not responded is a collection-stage status,
    # not a defect in extracted comparison data.
    unresolved = [f"{s['supplier']}: {issue}" for s in summaries if not s["manually_excluded"] for issue in s["blockers"]]
    excluded_warnings = [f"{s['supplier']}: {issue}" for s in summaries if s["manually_excluded"] for issue in s["blockers"]]
    critical_unresolved = [f"{s['supplier']}: {issue}" for s in summaries if not s["manually_excluded"]
                           for issue in s["blockers"] if issue != "No extracted quote"]
    totals = [s for s in summaries if s["total"] is not None and not s["manually_excluded"]]
    eligible_totals = [s for s in totals if s["eligible"]]
    def lowest(records):
        if not records:
            return None
        best = min(s["total"] for s in records)
        return {"suppliers": [s["supplier"] for s in records if s["total"] == best], "total": best}
    extracted_count = sum(summary["source"] is not None for summary in summaries)
    if extracted_count == 0:
        decision = "Awaiting supplier quotations. Extract demo sources or upload responses to begin the comparison."
    elif critical_unresolved:
        candidate = lowest(eligible_totals)
        if candidate:
            decision = (f"Current leading eligible bid: {', '.join(candidate['suppliers'])} at {rfx.base_currency} {candidate['total']:,.2f}, "
                        f"based on {len(eligible_totals)} complete eligible active bid(s). Final recommendation remains provisional because "
                        f"{len(critical_unresolved)} critical issue(s) remain on active suppliers. Resolve them or explicitly exclude those suppliers.")
        else:
            decision = (f"No eligible complete bid can be recommended yet. {len(critical_unresolved)} critical issue(s) remain on active suppliers. "
                        "Resolve them or explicitly exclude those suppliers.")
    else:
        candidate = lowest(eligible_totals)
        if candidate:
            decision = (f"Recommended for buyer approval: {', '.join(candidate['suppliers'])}, with the lowest eligible reviewed delivered cost of "
                        f"{rfx.base_currency} {candidate['total']:,.2f}. This recommendation reflects the saved review decisions, assumptions and exclusions; "
                        "the buyer retains final approval authority.")
        else:
            decision = "Decision not ready: no complete eligible supplier bid remains. Review eligibility exclusions or obtain revised supplier evidence."
    snapshot = {"base_currency": rfx.base_currency, "lines": rows, "suppliers": summaries, "quality": quality_table,
                "cheapest_lines": cheapest, "warnings": unresolved, "excluded_warnings": excluded_warnings,
                "critical_warnings": critical_unresolved, "lowest_total": lowest(totals),
                "lowest_eligible": lowest(eligible_totals), "assumptions": assumptions.model_dump(),
                "supplier_exclusions": supplier_exclusions,
                "decision": decision, "extracted_supplier_count": extracted_count,
                "cost_basis": "Pre-tax delivered cost. Line winners compare material only, before order discounts/freight. Split-award savings are not calculated."}
    fingerprint = hashlib.sha256(dumps(fingerprint_data).encode()).hexdigest()
    snapshot["dataset_version"] = fingerprint
    if audit and repo.get_state("normalization_version") != fingerprint:
        repo.log("normalized_snapshot", fingerprint, after=snapshot, reason="Deterministic normalization, review status, formulas and assumptions")
        repo.set_state("normalization_version", fingerprint, actor="system", reason="Snapshot version")
    return snapshot
