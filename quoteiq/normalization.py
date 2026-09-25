"""Deterministic matching and explicit conversions; no implicit FX or pack factors."""
import re
from decimal import Decimal, ROUND_HALF_UP
from .models import Assumptions, ExtractedLine, LineReview, QuoteTerms


def dec(value):
    return Decimal(str(value))


def money(value):
    return float(dec(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fx_rate(currency, base_currency, assumptions):
    if currency == base_currency:
        return Decimal(1), f"{base_currency} native; FX=1"
    for fx in assumptions.fx:
        if fx.currency == currency:
            return dec(fx.rate), f"1 {currency} = {fx.rate} {base_currency}; {fx.as_of}; {fx.source}"
    return None, f"Missing {currency or 'unknown currency'} to {base_currency} FX assumption"


def tax_factor(terms):
    if terms.tax_included is False:
        return Decimal(1)
    if terms.tax_included is True and terms.tax_percent is not None:
        return Decimal(1) + dec(terms.tax_percent) / 100
    return None


def normalize_line(line: ExtractedLine, review, rfx, terms: QuoteTerms, assumptions: Assumptions):
    ids = {i.id: i for i in rfx.items}
    codes = re.findall(r"\bPKG-\d{3}\b", line.raw_item.upper())
    exact = codes[0] if len(set(codes)) == 1 and codes[0] in ids else None
    item_id = exact or line.matched_item_id
    price, currency, basis, qty, spec = line.price, line.currency or terms.currency, line.price_basis, line.offered_quantity, line.spec_compliant
    issues, notes = [], []
    status = "pending"
    if review:
        checked = LineReview.model_validate(review)
        item_id, price, currency, basis, qty, spec = checked.item_id, checked.price, checked.currency, checked.price_basis, checked.offered_quantity, checked.spec_compliant
        status = checked.decision
    else:
        issues.append("Human review required")
        issues.extend(line.issues)
        if not exact:
            issues.append("Item match requires confirmation")
        if exact and line.matched_item_id and exact != line.matched_item_id:
            issues.append("Item-code and AI match conflict")
        if line.confidence < 0.85:
            issues.append("Low extraction confidence")
        if not line.evidence.locator or not line.evidence.excerpt:
            issues.append("Missing source evidence")
    if status == "rejected":
        issues.append("Rejected by reviewer")
    item = ids.get(item_id)
    if not item:
        issues.append("Unmatched RFx item")
    if price is None:
        issues.append("Missing price")
    if qty is None:
        issues.append("Missing quoted quantity")
    elif item and qty < item.quantity:
        issues.append("Insufficient quoted quantity")
    if spec is None:
        issues.append("Technical compliance unknown")
    rate, fx_note = fx_rate(currency, rfx.base_currency, assumptions)
    notes.append(fx_note)
    if rate is None:
        issues.append(fx_note)
    divisor = {"piece": 1, "100_pieces": 100}.get(basis)
    if basis in {"box", "100_boxes"} and assumptions.requested_units_per_box is not None:
        units_per_box = dec(assumptions.requested_units_per_box)
        divisor = units_per_box * (100 if basis == "100_boxes" else 1)
        notes.append(f"1 supplier box = {units_per_box} requested units: {assumptions.unit_reason}")
    if divisor is None:
        issues.append("Unknown price unit or box-to-piece assumption")
    tax = tax_factor(terms)
    if tax is None:
        issues.append("Unknown tax treatment")
    unit = dec(price) * rate / divisor / tax if price is not None and rate is not None and divisor and tax else None
    total = money(unit * item.quantity) if unit is not None and item else None
    return {
        "item_id": item_id, "description": item.description if item else line.raw_description,
        "quantity": item.quantity if item else None, "offered_quantity": qty,
        "raw_price": line.raw_price, "raw_unit": line.raw_unit, "raw_currency": line.raw_currency,
        "raw_item": line.raw_item, "raw_quantity": line.raw_quantity, "raw_spec": line.raw_spec,
        "effective_price": price, "currency": currency, "price_basis": basis,
        "unit_price": float(unit) if unit is not None else None,
        "unit_price_exact": str(unit) if unit is not None else None,
        "line_total": total, "review_status": status, "spec_compliant": spec,
        "confidence": line.confidence, "issues": list(dict.fromkeys(issues)),
        "conversion": "; ".join(notes), "source_locator": line.evidence.locator,
        "source_excerpt": line.evidence.excerpt,
        "formula": f"{price} {currency} × FX {rate} ÷ {divisor} ÷ tax factor {tax} × {item.quantity if item else '?'}",
    }
