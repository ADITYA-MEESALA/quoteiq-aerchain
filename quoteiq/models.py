"""Shared contracts. Missing facts stay null; never fill commercial facts."""
from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class LineItem(Model):
    id: str = Field(pattern=r"^PKG-\d{3}$")
    description: str = Field(min_length=1)
    spec: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    unit: Literal["piece"] = "piece"


class Question(Model):
    id: str
    question: str
    mandatory: bool = True


class RFx(Model):
    id: int = 1
    title: str = Field(min_length=1)
    brief: str
    base_currency: str = Field(pattern=r"^[A-Z]{3}$")
    delivery_location: str
    max_lead_days: int = Field(gt=0)
    commercial_terms: str
    items: list[LineItem] = Field(min_length=1, max_length=500)
    questionnaire: list[Question] = Field(min_length=1)
    origin: str = "AI generated"

    @model_validator(mode="after")
    def unique_ids(self):
        for records in (self.items, self.questionnaire):
            if len({r.id for r in records}) != len(records):
                raise ValueError("Item and questionnaire IDs must be unique")
        return self


class Supplier(Model):
    id: str
    name: str = Field(min_length=1)
    contact: str
    email: str
    location: str
    notes: str


class Evidence(Model):
    locator: str = Field(description="Exact provided page, paragraph, cell range, line or image region")
    excerpt: str = Field(description="Verbatim supporting source text; empty when unavailable")


class ExtractedLine(Model):
    raw_item: str
    raw_description: str
    raw_price: str
    raw_currency: str
    raw_unit: str
    raw_quantity: str
    raw_spec: str
    matched_item_id: str | None
    price: float | None = Field(ge=0)
    currency: str | None
    price_basis: Literal["piece", "box", "100_pieces", "100_boxes", "unknown"]
    offered_quantity: int | None = Field(ge=0)
    spec_compliant: bool | None
    confidence: float = Field(ge=0, le=1)
    issues: list[str]
    evidence: Evidence


class QualityAnswer(Model):
    question_id: str
    raw_answer: str
    meets_requirement: bool | None
    evidence: Evidence


class QuoteTerms(Model):
    currency: str | None
    freight: float | None = Field(ge=0)
    raw_freight: str
    discount_percent: float | None = Field(ge=0, le=100)
    discount_kind: Literal["none", "unconditional", "minimum_order", "other", "unknown"]
    discount_threshold: float | None = Field(ge=0)
    raw_discount: str
    lead_days: int | None = Field(ge=0)
    raw_lead_time: str
    payment_terms: str
    tax_included: bool | None
    tax_percent: float | None = Field(ge=0, le=100)
    raw_tax: str
    other_conditions: str
    evidence: list[Evidence]


class ExtractedQuote(Model):
    supplier_name: str | None
    lines: list[ExtractedLine]
    terms: QuoteTerms
    quality: list[QualityAnswer]
    warnings: list[str]


class LineReview(Model):
    item_id: str | None
    price: float | None = Field(ge=0)
    currency: str | None = Field(pattern=r"^[A-Z]{3}$")
    price_basis: Literal["piece", "box", "100_pieces", "100_boxes", "unknown"]
    offered_quantity: int | None = Field(ge=0)
    spec_compliant: bool | None
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=3)

    @model_validator(mode="after")
    def approved_has_facts(self):
        if self.decision == "approved" and (
            any(x is None for x in (self.item_id, self.price, self.currency, self.offered_quantity))
            or self.price_basis == "unknown"
        ):
            raise ValueError("Approval needs an item match, price, currency, known unit and quantity")
        return self


class TermsReview(Model):
    terms: QuoteTerms
    quality: list[QualityAnswer]
    discount_decision: Literal["auto", "apply", "ignore"]
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=3)


class FxRate(Model):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    rate: float = Field(gt=0)
    as_of: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    source: str = Field(min_length=3)

    @field_validator("as_of")
    @classmethod
    def real_date(cls, value):
        date.fromisoformat(value)
        return value


class Assumptions(Model):
    fx: list[FxRate] = []
    box_is_piece: bool = False
    requested_units_per_box: float | None = Field(default=None, gt=0)
    unit_reason: str = ""

    @model_validator(mode="after")
    def validate_assumptions(self):
        if len({x.currency for x in self.fx}) != len(self.fx):
            raise ValueError("Only one FX rate per currency is allowed")
        # Preserve workspaces saved before configurable box pack sizes existed.
        if self.box_is_piece and self.requested_units_per_box is None:
            self.requested_units_per_box = 1
        if self.requested_units_per_box is not None and len(self.unit_reason.strip()) < 3:
            raise ValueError("Document the source for the box-to-requested-unit conversion")
        self.box_is_piece = self.requested_units_per_box == 1
        return self
