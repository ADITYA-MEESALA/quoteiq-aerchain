# Validation record

Executed in this workspace on **2026-09-23** with Python 3.12 on Windows.

| Check | Actual result |
| --- | --- |
| Non-live pytest suite | **61 passed, 2 skipped** in 30.10 seconds on 2026-09-24; both live tests skip unless explicitly enabled |
| Live Gemini RFx plus five formats | **1 passed** in 200.91 seconds with `gemini-3.5-flash-lite` |
| Live grounded Gemini analyst | **1 passed, 1 deselected** in 22.00 seconds |
| Streamlit health | Fresh process started on `127.0.0.1:8501`; health and root both returned HTTP 200 (`ok`) |
| Docker/public deployment | Not run; Docker is not installed here |

The live extraction test generated a new 30-item RFx, created the five XLSX/PDF/DOCX/JPG/email sources, and sent every source through the production ingestion route. It verified 30 lines for four suppliers, 27 for the incomplete email, source locators, the USD quotation, and the PDF's conditional advance-payment discount. No extracted result came from a fixture or answer sidecar.

The live analyst test used a reviewed deterministic dataset. Gemini produced a Pydantic-validated query plan, the Python calculation layer executed the allowlisted operation, and Gemini answered from the returned evidence. Application code never executes model-written SQL or Python.

Initial `gemini-3.5-flash` runs exposed JSON Schema `const` incompatibility and repeated HTTP 503 responses. Literal values are now represented as one-value enums, images are transmitted as Gemini `inlineData`, and transient 429/5xx responses receive bounded retries. After selecting `gemini-3.5-flash-lite`, the complete live workflow passed.

Deterministic coverage includes FX and unit assumptions, tax normalization, Decimal rounding, partial quotes, discount conditions, missing freight/quality, eligibility, RFx revision invalidation, corrections, ties, append-only audit, revisions, analyst validation, export escaping, provider routing, and Gemini image/schema contracts. Production ingestion has no offline extraction fallback.

Gemini free-tier processing is appropriate for the fictional assignment dataset. Google states that free-tier content may be used to improve its products, so confidential supplier material should not be loaded under this setup without an approved data arrangement.

The required screen-recorded analyst walkthrough is still outstanding. The repository includes a recording script, not a substitute video.
