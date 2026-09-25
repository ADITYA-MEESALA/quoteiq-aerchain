"""Streamlit presentation. Domain rules live in services, never in widgets."""
import io
import json
import os
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile
import pymupdf as fitz
import pandas as pd
import streamlit as st
from . import analyst, demo_data, llm
from .calculations import build_snapshot
from .db import Repository, now
from .exports import comparison_workbook, csv_bytes, evidence_zip, rfx_workbook
from .extractors import ingest
from .models import Assumptions, ExtractedLine, ExtractedQuote, LineReview, RFx, Supplier, TermsReview
from .review import add_manual_quote_line, approval_missing_fields, approve_verified_batch, batch_candidates, guided_demo_review, quote_lines, review_sources_bulk, save_line_review, save_terms_review

PAGES = ["Overview", "RFx studio", "Suppliers", "Quote inbox", "Review desk", "Compare & decide", "AI analyst", "Audit trail"]
CSS = """
<style>
.block-container {max-width:1440px;padding-top:2rem;padding-bottom:3rem;}
h1,h2,h3 {letter-spacing:-.04em;color:#163c2d;}
h1 {font-size:2.55rem!important;font-weight:750!important;}
[data-testid="stSidebar"] {border-right:1px solid #dce4d9;}
[data-testid="stMetric"] {background:white;border:1px solid #dfe7df;border-radius:14px;padding:18px 20px;}
[data-testid="stMetricValue"] {color:#145840;}
.eyebrow {display:inline-flex;align-items:center;font-size:11px;line-height:1;letter-spacing:1.8px;text-transform:uppercase;color:#174b37!important;font-weight:800;background:#e2f0e6;border:1px solid #bad5c2;border-radius:999px;padding:8px 12px;margin:0 0 14px;box-shadow:0 1px 2px rgba(18,48,39,.08);}
.hero {padding:28px 32px;background:#173e30;color:#edf7ef;border-radius:18px;margin:8px 0 24px;}
.hero h2 {color:#f4f9ee;font-size:30px;margin:0 0 8px;letter-spacing:-1px;}
.hero p {color:#c2d7c8;margin:0;max-width:850px;line-height:1.6;}
.step {border-top:3px solid #80a28e;background:white;padding:16px 18px;border-radius:3px 3px 12px 12px;min-height:125px;}
.step b {display:block;font-size:16px;margin:6px 0;}.step small {color:#6a7a70;line-height:1.6;}
.brand {font-size:30px;font-weight:800;letter-spacing:-1.5px;color:#163c2d;}
.brand span {color:#549373;}.subbrand {font-size:10px;letter-spacing:2.5px;color:#6d8476;margin:0 0 30px;}
div.stButton > button {border-radius:8px;font-weight:600;}
[data-testid="stDataFrame"] {border-radius:10px;}
</style>
"""


def show_error(exc):
    st.error(llm.safe_error(exc))


def frame(records):
    return pd.DataFrame(records)


def lowest_visible(summaries, eligible_only=False):
    candidates = [summary for summary in summaries if summary.get("total") is not None
                  and (not eligible_only or summary.get("eligible"))]
    if not candidates:
        return None
    best = min(summary["total"] for summary in candidates)
    return {"suppliers": [summary["supplier"] for summary in candidates if summary["total"] == best], "total": best}


def blocker_action(issue):
    text = issue.lower()
    if "no extracted quote" in text:
        return "Quote inbox → extract the source"
    if "fx assumption" in text or "box-to-piece" in text or "price unit" in text:
        return "Review desk → Currency and unit assumptions"
    if "discount" in text or "freight" in text or "payment" in text or "tax" in text or "commercial terms" in text or "validity" in text:
        return "Review desk → Commercial & quality"
    if "quality" in text or "lead time" in text:
        return "Review desk → Commercial & quality"
    if "incomplete" in text:
        return "Request a revised quote, then use Quote inbox"
    if "not all line costs" in text:
        return "Resolve the related line, FX, unit or terms blocker"
    return "Review desk → Line items"


def issue_guidance(issue):
    text = issue.lower()
    if "buyer excluded supplier" in text:
        return "This supplier was explicitly excluded by a buyer. Use the eligibility decision below to restore rule-based evaluation if the exclusion no longer applies."
    if "lead time exceeds" in text or "delivery lead time" in text:
        return ("The quoted commitment exceeds the RFx maximum. Enter a shorter lead time only when a revised supplier response supports it; "
                "otherwise keep this supplier ineligible and compare the other bids.")
    if "quality requirement failed, unknown or unreviewed" in text:
        question = issue.split(":", 1)[0]
        return (f"Verify {question} under Commercial & quality. Mark it Meets only when the quotation, certificate, test report or supplier clarification supports it. "
                "A stated No or Unknown correctly keeps the supplier ineligible.")
    if "technical requirements not met" in text:
        return "Change the technical verdict only from revised supplier evidence. A genuine noncompliance should remain an eligibility exclusion."
    if "no extracted quote" in text:
        return "Extract or upload the supplier response. No review decision is possible until a source has been processed."
    if "incomplete or duplicate quote" in text:
        return "Correct a duplicate match or obtain the missing RFx line from the supplier. Rejecting the only valid line leaves the bid incomplete."
    if "discount" in text:
        return "Review the exact discount conditions and choose Apply or Ignore. Apply only when every stated condition is satisfied."
    if "fx assumption" in text:
        return "Enter a dated, buyer-approved conversion rate and its source. QuoteIQ will not fetch or invent a rate."
    if "line-level" in text or "price unit" in text:
        return "Open the highlighted affected rows, correct only values supported by the original source, then approve and save them."
    return "Review the original evidence, correct the interpreted value if supported, and save the decision with a reason."


def blocker_table(snapshot):
    rows = []
    expected_items = {row.get("item_id") for row in snapshot.get("cheapest_lines", []) if row.get("item_id")}
    for supplier in snapshot.get("suppliers", []):
        excluded = supplier.get("manually_excluded", False)
        supplier_lines = [line for line in snapshot.get("lines", [])
                          if line.get("supplier_id") == supplier["supplier_id"] and line.get("review_status") != "rejected"]
        issue_counts = {}
        for line in supplier_lines:
            for line_issue in line.get("issues", []):
                issue_counts[line_issue] = issue_counts.get(line_issue, 0) + 1
        issue_detail = "; ".join(f"{count}× {name}" for name, count in
                                 sorted(issue_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:5])
        for issue in supplier.get("blockers", []):
            detail = ""
            if issue == "Line-level review or normalization issues remain":
                detail = issue_detail or "Open the supplier's lines to inspect the unresolved fields."
            elif issue == "Not all line costs are known":
                unknown = sum(line.get("line_total") is None for line in supplier_lines)
                detail = f"{unknown}/{len(supplier_lines)} extracted active line(s) have no normalized cost. Fix the related review, FX, unit or commercial blocker first."
            elif issue.startswith("Incomplete or duplicate quote"):
                active_ids = [line.get("item_id") for line in supplier_lines if line.get("item_id")]
                missing = sorted(expected_items - set(active_ids))
                duplicates = sorted({item_id for item_id in active_ids if active_ids.count(item_id) > 1})
                parts = []
                if missing:
                    parts.append("Missing after review: " + ", ".join(missing))
                if duplicates:
                    parts.append("Duplicated: " + ", ".join(duplicates))
                parts.append("A rejected line is excluded from coverage; correct and approve it if it is a valid quote line.")
                detail = ". ".join(parts)
            issue_type = ("Excluded supplier issue" if excluded else
                          "Awaiting quote" if issue == "No extracted quote" else "Critical blocker")
            rows.append({"Supplier": supplier["supplier"], "Type": issue_type, "Issue": issue,
                         "Details": detail, "Resolve in": blocker_action(issue)})
        for issue in supplier.get("eligibility_reasons", []):
            issue_type = "Manual exclusion" if issue.startswith("Buyer excluded supplier:") else ("Excluded supplier eligibility" if excluded else "Eligibility")
            rows.append({"Supplier": supplier["supplier"], "Type": issue_type, "Issue": issue,
                         "Details": issue_guidance(issue), "Resolve in": blocker_action(issue)})
    return rows


def line_blocker_table(snapshot):
    rows = []
    excluded_ids = {supplier["supplier_id"] for supplier in snapshot.get("suppliers", []) if supplier.get("manually_excluded")}
    for line in snapshot.get("lines", []):
        if line.get("review_status") == "rejected" or line.get("supplier_id") in excluded_ids:
            continue
        for issue in line.get("issues", []):
            rows.append({
                "Supplier": line.get("supplier", ""),
                "Line": (line.get("line_index", 0) + 1),
                "RFx item": line.get("item_id") or "Unmatched",
                "Raw supplier item": line.get("raw_item", ""),
                "Issue": issue,
                "Source": line.get("source_locator", ""),
                "Resolve in": blocker_action(issue),
            })
    return rows


def show_blockers(snapshot, expanded=True):
    all_rows = blocker_table(snapshot)
    excluded_types = {"Excluded supplier issue", "Excluded supplier eligibility", "Manual exclusion"}
    rows = [row for row in all_rows if row["Type"] not in excluded_types]
    if not rows:
        st.success("No unresolved comparison or eligibility issues.")
        excluded_count = sum(s.get("manually_excluded", False) for s in snapshot.get("suppliers", []))
        if excluded_count:
            st.caption(f"{excluded_count} manually excluded supplier(s) are omitted from active blockers and comparisons. Use Resolve blockers here to restore them.")
        return
    critical = sum(row["Type"] == "Critical blocker" for row in rows)
    if critical:
        st.warning(f"Action required: {critical} critical comparison issue(s). Open the table below to see where each one can be resolved.")
    else:
        awaiting = sum(row["Type"] == "Awaiting quote" for row in rows)
        message = (f"Quote collection is in progress: {awaiting} supplier response(s) still awaited. No extracted-data blockers exist yet."
                   if awaiting else "No critical comparison issues. Review the eligibility notes below before making a decision.")
        st.info(message)
    with st.expander("What needs attention", expanded=expanded):
        st.dataframe(frame(rows), hide_index=True, width="stretch")
        line_rows = line_blocker_table(snapshot)
        if line_rows:
            st.markdown("**Exact line items to correct**")
            st.caption("The Line number matches the Line column in Review desk → Line items. Use the table search to find a supplier, RFx item or issue.")
            st.dataframe(frame(line_rows), hide_index=True, width="stretch", height=min(520, 38 + 35 * len(line_rows)))
        st.caption("Eligibility issues may remain after a quote becomes calculable. Missing supplier information requires a revised quote; QuoteIQ does not invent it.")
        excluded_count = sum(s.get("manually_excluded", False) for s in snapshot.get("suppliers", []))
        if excluded_count:
            st.caption(f"{excluded_count} manually excluded supplier(s) are omitted from this active issue list and comparison tables.")


def optional_number(value, integer=False):
    if value is None or str(value).strip() in {"", "None", "nan", "<NA>"}:
        return None
    return int(value) if integer else float(value)


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return value


def prepare_demo(repo):
    if repo.get_state("demo_run_id") is None:
        repo.set_state("demo_run_id", uuid4().hex[:8], actor="system", reason="New fictional demo run")
        repo.set_state("demo_created_at", now(), actor="system", reason="New fictional demo run")
    if repo.rfx() is None:
        repo.set_state("rfx", demo_data.generate_rfx().model_dump(), reason="Explicit fictional demo seed; not AI generated")
    if not repo.suppliers():
        repo.set_state("suppliers", [s.model_dump() for s in demo_data.demo_suppliers()])
    paths = demo_data.save_demo_files(repo.root / "demo" / repo.rfx_hash()[:12], repo.rfx())
    repo.set_state("demo_manifest", {sid: str(p.resolve()) for sid, p in paths.items()}, reason="Generated fictional source files only")
    repo.set_state("demo_rfx_hash", repo.rfx_hash())


def overview(repo, snapshot, actor):
    st.markdown('<div class="eyebrow">Sourcing workspace&nbsp;&nbsp;/&nbsp;&nbsp;Aerchain Builder</div>', unsafe_allow_html=True)
    st.title("Good decisions start with clear quotes.")
    flash = st.session_state.pop("workspace_flash", None)
    if flash:
        st.success(flash)
        st.toast(flash, icon="✅")
    st.markdown('<div class="hero"><h2>Kill the quote spreadsheet.</h2><p>One RFx. Five supplier formats. A comparison you can trace back to the source. Draft your requirements, bring in the quotes, and resolve the details before deciding.</p></div>', unsafe_allow_html=True)
    rfx = repo.rfx()
    cols = st.columns(4)
    cols[0].metric("RFx line items", len(rfx.items) if rfx else 0)
    cols[1].metric("Suppliers", len(repo.suppliers()))
    cols[2].metric("Quotes extracted", len(repo.documents(active_only=True)))
    cols[3].metric("Eligible full bids", sum(s["eligible"] for s in snapshot["suppliers"]))
    st.write("")
    for col, (n, title, detail) in zip(st.columns(4), [
        ("01", "Define the ask", "Generate and edit 30 items, terms and quality requirements."),
        ("02", "Collect the evidence", "Invite suppliers and extract their original quote files."),
        ("03", "Resolve the details", "Confirm matches, units, FX and commercial conditions."),
        ("04", "Compare with confidence", "Inspect costs, eligibility, calculations and sources.")]):
        col.markdown(f'<div class="step"><small>{n}</small><b>{title}</b><small>{detail}</small></div>', unsafe_allow_html=True)
    st.write("")
    milestones = [bool(rfx), bool(repo.documents(active_only=True)), any(repo.reviews(d["id"]) for d in repo.documents(active_only=True)),
                  any(s["eligible"] for s in snapshot.get("suppliers", []))]
    completed = sum(milestones)
    st.progress(completed / 4, text=f"Workflow readiness · {completed}/4 stages complete")
    left, right = st.columns([1.4, 1])
    with left:
        st.subheader("Start with the packaging scenario")
        st.write("The demo includes five fictional suppliers and five deliberately different source documents. Preparing it creates the RFx and original files; extraction uses the configured live AI provider.")
        if st.button("Prepare demo workspace", type="primary"):
            try:
                prepare_demo(repo)
                st.success("30-item RFx and five source files are ready. Continue to Suppliers, then Quote inbox.")
                st.rerun()
            except Exception as exc:
                show_error(exc)
        if rfx:
            st.caption(f"Active RFx: {rfx.title} · {rfx.origin}")
        run_id = repo.get_state("demo_run_id")
        if run_id:
            review_count = sum(len(repo.reviews(d["id"])) for d in repo.documents())
            st.caption(f"Demo run {run_id} · created {repo.get_state('demo_created_at', '')[:19]} · {len(repo.documents())} stored quote revision(s) · {review_count} saved review record(s)")
        with st.expander("Start the demo again from scratch"):
            st.warning("This permanently clears the current RFx, quotations, reviews, assumptions and audit history in this local demo workspace.")
            reset_text = st.text_input("Type RESET to confirm", key="reset_demo_confirmation")
            if st.button("Reset and prepare fresh demo", disabled=reset_text != "RESET", type="secondary"):
                try:
                    repo.reset_workspace()
                    prepare_demo(repo)
                    st.session_state.clear()
                    st.session_state["workspace_flash"] = (f"Fresh demo {repo.get_state('demo_run_id')} created. Previous quotations, reviews, assumptions and audit history were deleted. Quotes extracted: 0 · Saved reviews: 0.")
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
    with right:
        st.subheader("Decision readiness")
        st.info(snapshot["decision"])
        st.caption("Invitations are simulated. AI drafting, extraction and analysis require a configured provider key. Reviews and calculations are saved locally.")
        if not llm.configured():
            st.warning("AI is not configured. Set AI_PROVIDER and its API key in .env, then restart.")
    if rfx:
        show_blockers(snapshot, expanded=True)


def rfx_studio(repo, snapshot, actor):
    st.title("RFx studio")
    st.caption("Turn a procurement brief into a structured request. Everything stays editable.")
    with st.expander("Draft with AI", expanded=repo.rfx() is None):
        brief = st.text_area("Procurement brief", demo_data.BRIEF, height=125)
        if st.button("Generate 30-item RFx with AI", type="primary", disabled=not llm.configured()):
            try:
                with st.spinner("Drafting items, specifications, terms and quality questions…"):
                    rfx, metadata = llm.generate_rfx(brief)
                    repo.set_state("rfx", rfx.model_dump(), actor, "AI draft from buyer brief")
                    repo.log("rfx_generation", "rfx", after=metadata, actor=metadata.get("provider", "AI"))
                st.rerun()
            except Exception as exc:
                show_error(exc)
    rfx = repo.rfx()
    if rfx is None:
        st.info("Generate an RFx above, or prepare the explicitly fictional seed from Overview.")
        return
    st.caption(f"Origin: {rfx.origin}")
    if repo.documents():
        st.warning("Saving changes creates a new RFx revision. Earlier quotes remain in the audit trail and must be re-uploaded for comparison.")
    with st.form("rfx_editor"):
        title = st.text_input("RFx title", rfx.title)
        c1, c2, c3 = st.columns([2, 1, 1])
        location = c1.text_input("Delivery location", rfx.delivery_location)
        currency = c2.text_input("Base currency", rfx.base_currency)
        lead = c3.number_input("Maximum lead days", min_value=1, value=rfx.max_lead_days)
        terms = st.text_area("Commercial terms", rfx.commercial_terms, height=100)
        guided = st.session_state.get("experience", "Guided demo") == "Guided demo"
        st.subheader("30 line items" if guided else "RFx line items")
        items = st.data_editor(frame([x.model_dump() for x in rfx.items]), hide_index=True, width="stretch", height=400,
                               disabled=["id", "unit"], key="rfx_items", num_rows="fixed" if guided else "dynamic",
                               column_config={"quantity": st.column_config.NumberColumn(min_value=1, step=1)})
        st.subheader("Supplier quality questionnaire")
        questions = st.data_editor(frame([x.model_dump() for x in rfx.questionnaire]), hide_index=True, width="stretch", num_rows="dynamic", key="rfx_questions")
        save = st.form_submit_button("Save RFx", type="primary")
    if save:
        try:
            value = rfx.model_dump()
            value.update(title=title, delivery_location=location, base_currency=currency.strip().upper(), max_lead_days=int(lead), commercial_terms=terms,
                         items=clean(items.to_dict("records")), questionnaire=clean(questions.to_dict("records")))
            updated = RFx.model_validate(value)
            repo.set_state("rfx", updated.model_dump(), actor, "Buyer edited RFx")
            st.success("RFx saved.")
        except Exception as exc:
            show_error(exc)
    st.download_button("Download RFx workbook", rfx_workbook(repo.rfx()), "QuoteIQ-RFx.xlsx")


def suppliers_page(repo, snapshot, actor):
    st.title("Supplier network")
    guided = st.session_state.get("experience", "Guided demo") == "Guided demo"
    st.caption("Five fictional packaging suppliers. Edit contacts and prepare a traceable invitation round." if guided else
               "Maintain the suppliers participating in this sourcing event and prepare a traceable invitation round.")
    with st.form("suppliers"):
        columns = ["id", "name", "contact", "email", "location", "notes"]
        supplier_frame = frame([s.model_dump() for s in repo.suppliers()]) if repo.suppliers() else pd.DataFrame(columns=columns)
        edited = st.data_editor(supplier_frame, hide_index=True, width="stretch", disabled=["id"] if guided else [], height=250,
                                num_rows="fixed" if guided else "dynamic")
        save = st.form_submit_button("Save supplier details")
    if save:
        try:
            values = [Supplier.model_validate(x).model_dump() for x in clean(edited.to_dict("records"))]
            if not values or len({x["id"] for x in values}) != len(values) or len({x["name"] for x in values}) != len(values):
                raise ValueError("Use at least one supplier with distinct IDs and names")
            if guided and len(values) != 5:
                raise ValueError("Guided demo uses exactly five suppliers")
            repo.set_state("suppliers", values, actor, "Buyer edited supplier directory")
            st.success("Supplier details saved.")
        except Exception as exc:
            show_error(exc)
    if not repo.rfx():
        st.info("Create an RFx to prepare invitations.")
        return
    st.subheader("Invitation round")
    st.info("Simulation: invitations are recorded in this workspace. No emails are sent.")
    chosen = st.multiselect("Recipients", [s.id for s in repo.suppliers()], default=[s.id for s in repo.suppliers()], format_func=lambda sid: next(s.name for s in repo.suppliers() if s.id == sid))
    if st.button("Simulate RFx invitations", type="primary", disabled=not chosen):
        invitations = repo.get_state("invitations", [])
        for s in repo.suppliers():
            if s.id in chosen:
                invitations.append({"supplier_id": s.id, "supplier": s.name, "email": s.email, "rfx_hash": repo.rfx_hash(), "timestamp": now(), "status": "Simulated invitation"})
        repo.set_state("invitations", invitations, actor, "Explicitly simulated; no external message")
        st.success(f"Recorded {len(chosen)} simulated invitations.")
    with st.expander("Invitation preview"):
        st.text(f"Subject: RFx invitation — {repo.rfx().title}\n\nPlease quote all 30 attached items, commercial terms and quality questions.\nDelivery: {repo.rfx().delivery_location}\n{repo.rfx().commercial_terms}")
        st.download_button("Download invitation attachment", rfx_workbook(repo.rfx()), "QuoteIQ-invitation.xlsx")
    if repo.get_state("invitations"):
        st.dataframe(frame(repo.get_state("invitations")), hide_index=True, width="stretch")


def inbox(repo, snapshot, actor):
    st.title("Quote inbox")
    st.caption("Keep the original. Extract the evidence. Review before comparing.")
    if repo.rfx() is None:
        st.info("Create an RFx first.")
        return
    demo_tab, upload_tab, email_tab = st.tabs(["Demo documents", "Upload a quote", "Paste an email"])
    with demo_tab:
        st.write("Excel with unusual headers · conditional-discount PDF · USD Word quotation · simulated phone photo · incomplete email")
        if st.button("Generate source files for current RFx"):
            prepare_demo(repo)
            st.success("Fictional source files generated.")
        manifest = repo.get_state("demo_manifest", {})
        current = repo.get_state("demo_rfx_hash") == repo.rfx_hash()
        if manifest and not current:
            st.warning("Demo documents were generated for an earlier RFx. Generate them again.")
        if manifest and current:
            blob = io.BytesIO()
            with ZipFile(blob, "w", ZIP_DEFLATED) as archive:
                for sid, path in manifest.items():
                    source = Path(path)
                    if source.exists():
                        archive.write(source, source.name)
            st.download_button("Download all five originals", blob.getvalue(), "QuoteIQ-demo-sources.zip")
            st.caption("Extraction sends source content to the configured provider. Free-tier quotas and provider data policies apply. It does not load prewritten answers.")
            if st.button("Extract all five with AI", type="primary", disabled=not llm.configured()):
                with st.status("Extracting five supplier quotes…", expanded=True) as status:
                    failures = 0
                    for sid, path in manifest.items():
                        source = Path(path)
                        st.write(f"Reading {source.name}")
                        try:
                            doc_id = ingest(repo, sid, source.name, source.read_bytes())
                            doc = repo.document(doc_id)
                            if doc["status"] == "failed":
                                failures += 1
                                st.error(doc["error"])
                            else:
                                st.write(f"Extracted {len(json.loads(doc['extraction'])['lines'])} lines; review required.")
                        except Exception as exc:
                            failures += 1
                            show_error(exc)
                    status.update(label=f"Extraction finished: {5-failures} succeeded, {failures} failed", state="error" if failures else "complete")
    with upload_tab:
        suppliers = repo.suppliers()
        sid = st.selectbox("Supplier", [s.id for s in suppliers], format_func=lambda x: next(s.name for s in suppliers if s.id == x), key="upload_supplier")
        upload = st.file_uploader("Quote file", type=["xlsx", "pdf", "docx", "jpg", "jpeg", "png", "txt", "eml"])
        st.caption("Maximum 20 MB; PDFs up to 12 pages. A successful upload becomes the active quote revision for this supplier.")
        if st.button("Save and extract quote", disabled=upload is None, type="primary"):
            with st.spinner("Preserving the original and extracting its contents…"):
                doc_id = ingest(repo, sid, upload.name, upload.getvalue())
            doc = repo.document(doc_id)
            if doc["status"] == "failed":
                st.error(doc["error"])
            else:
                st.success("Quote extracted. Open Review desk to verify it.")
    with email_tab:
        sid = st.selectbox("Email supplier", [s.id for s in repo.suppliers()], format_func=lambda x: next(s.name for s in repo.suppliers() if s.id == x))
        text = st.text_area("Paste the original email", height=230)
        if st.button("Save and extract email", disabled=not text.strip()):
            with st.spinner("Extracting email…"):
                doc_id = ingest(repo, sid, "pasted-email.txt", text.encode("utf-8"))
            doc = repo.document(doc_id)
            if doc["status"] == "failed":
                st.error(doc["error"])
            else:
                st.success("Email saved and extracted. Missing values remain missing.")
    st.subheader("Source register")
    docs = repo.documents()
    if docs:
        st.dataframe(frame([{k: d[k] for k in ("supplier_id", "filename", "status", "active", "created_at", "error")} for d in docs]), hide_index=True, width="stretch")
        with st.expander("Retry a failed source"):
            failed = [d for d in docs if d["status"] == "failed"]
            if failed:
                selected = st.selectbox("Failed source", [d["id"] for d in failed], format_func=lambda x: next(d["filename"] for d in failed if d["id"] == x))
                if st.button("Retry as a new revision", disabled=not llm.configured()):
                    old = repo.document(selected)
                    with st.spinner("Retrying preserved original…"):
                        new_id = ingest(repo, old["supplier_id"], old["filename"], Path(old["path"]).read_bytes())
                    new = repo.document(new_id)
                    st.error(new["error"]) if new["status"] == "failed" else st.success("Retry succeeded.")


def source_view(doc):
    data = Path(doc["path"]).read_bytes()
    st.download_button("Download original", data, doc["filename"], key="original_" + doc["id"])
    st.caption(f"SHA-256: {doc['sha256']}")
    ext = Path(doc["filename"]).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png"}:
        st.image(data, caption=doc["filename"], width="stretch")
    elif ext == ".pdf":
        with fitz.open(stream=data, filetype="pdf") as pdf:
            page = st.number_input("Source page", min_value=1, max_value=len(pdf), value=1, key="pdf_page_" + doc["id"])
            st.image(pdf[page-1].get_pixmap(matrix=fitz.Matrix(1.3, 1.3)).tobytes("png"), width="stretch")
    parsed = json.loads(doc["parsed"] or "{}")
    if parsed.get("text"):
        st.text_area("Source text with locators", parsed["text"], height=350, disabled=True, key="source_text_" + doc["id"])


def review_lines(repo, doc, quote, actor, focus_indexes=None, highlight_attention=False):
    reviews = repo.reviews(doc["id"])
    records = []
    all_lines = quote_lines(repo, doc["id"], quote)
    for idx, line in enumerate(all_lines):
        if focus_indexes is not None and idx not in focus_indexes:
            continue
        rev = reviews.get(str(idx), {})
        records.append({"save": False, "line": idx + 1, "raw_item": line.raw_item, "raw_description": line.raw_description,
                        "raw_price": line.raw_price, "raw_currency": line.raw_currency, "raw_unit": line.raw_unit,
                        "raw_quantity": line.raw_quantity, "raw_spec": line.raw_spec,
                        "item_id": rev.get("item_id", line.matched_item_id), "price": rev.get("price", line.price),
                        "currency": rev.get("currency", line.currency or quote.terms.currency), "price_basis": rev.get("price_basis", line.price_basis),
                        "offered_quantity": rev.get("offered_quantity", line.offered_quantity),
                        "technical": {True: "Yes", False: "No", None: "Unknown"}[rev.get("spec_compliant", line.spec_compliant)],
                        "decision": rev.get("decision", "pending"), "confidence": line.confidence, "issues": "; ".join(line.issues),
                        "source": line.evidence.locator, "excerpt": line.evidence.excerpt})
    if not records:
        if focus_indexes is not None and all_lines:
            st.success("No line-specific reviews remain for this supplier. Check Commercial & quality for other blockers.")
        else:
            st.warning("The model found no quote lines. Review the original or upload a clearer revision.")
        return
    st.caption("Edit interpreted values, choose approved or rejected, and tick Save for each row you verified. Raw source values remain immutable.")
    if highlight_attention:
        valid_ids = {item.id for item in repo.rfx().items}
        preview = frame(records).copy()
        attention = []
        styles = pd.DataFrame("", index=preview.index, columns=preview.columns)
        for row_index, row in preview.iterrows():
            fields = []
            checks = {
                "item_id": not row["item_id"] or row["item_id"] not in valid_ids,
                "price": row["price"] is None or pd.isna(row["price"]),
                "currency": not row["currency"],
                "price_basis": row["price_basis"] == "unknown",
                "offered_quantity": row["offered_quantity"] is None or pd.isna(row["offered_quantity"]),
                "technical": row["technical"] == "Unknown",
                "decision": row["decision"] != "approved",
                "issues": bool(row["issues"]),
                "source": not row["source"],
                "excerpt": not row["excerpt"],
            }
            if "match" in str(row["issues"]).lower():
                checks["item_id"] = True
            if "confidence" in str(row["issues"]).lower():
                checks["confidence"] = True
            for column, needs_attention in checks.items():
                if needs_attention:
                    styles.loc[row_index, column] = "background-color: #fff0b3; color: #5b4300; font-weight: 600"
                    fields.append(column.replace("_", " "))
            attention.append(", ".join(fields) or "Review source evidence")
        preview.insert(2, "needs_human_review", attention)
        styles.insert(2, "needs_human_review", "background-color: #fff0b3; color: #5b4300; font-weight: 600")
        st.markdown("**Highlighted evidence view**")
        st.caption("Amber cells are the exact values that need human attention. Use the editable grid immediately below to correct them.")
        st.dataframe(preview.style.apply(lambda _: styles, axis=None), hide_index=True, width="stretch", height=min(420, 38 + 35 * len(preview)))
        st.markdown("**Edit and save these same rows**")
    grid_key = ("compare_review_grid_" if highlight_attention else "review_grid_") + doc["id"]
    version_key = grid_key + "_version"
    draft_key = grid_key + "_draft"
    if draft_key in st.session_state:
        records = st.session_state[draft_key]
    notice = st.session_state.pop(grid_key + "_notice", None)
    if notice:
        message = notice["message"]
        if notice["kind"] == "warning":
            st.warning(message)
        elif notice["kind"] == "success":
            st.success(message)
        else:
            st.info(message)
        st.toast(message, icon=notice.get("icon", "ℹ️"))
    with st.form("line_review_" + doc["id"]):
        edited = st.data_editor(frame(records), hide_index=True, width="stretch", height=460,
            disabled=["line", "raw_item", "raw_description", "raw_price", "raw_currency", "raw_unit", "raw_quantity", "raw_spec",
                      "confidence", "issues", "source", "excerpt"],
            column_config={"item_id": st.column_config.SelectboxColumn(options=[i.id for i in repo.rfx().items]),
                           "technical": st.column_config.SelectboxColumn(options=["Yes", "No", "Unknown"]),
                           "decision": st.column_config.SelectboxColumn(options=["pending", "approved", "rejected"]),
                           "price_basis": st.column_config.SelectboxColumn(options=["piece", "box", "100_pieces", "100_boxes", "unknown"]),
                           "price": st.column_config.NumberColumn(min_value=0, format="%.4f"),
                           "offered_quantity": st.column_config.NumberColumn(min_value=0, step=1)},
            key=f"{grid_key}_{st.session_state.get(version_key, 0)}")
        reason = st.text_input("Review / correction reason", placeholder="Describe what you verified or corrected against the source")
        select_col, approve_col, reject_col, save_col = st.columns(4)
        select_all = select_col.form_submit_button("Select all", use_container_width=True)
        approve_all = approve_col.form_submit_button("Approve all", use_container_width=True)
        reject_all = reject_col.form_submit_button("Reject all", use_container_width=True)
        save = save_col.form_submit_button("Save selected reviews", type="primary", use_container_width=True)
        st.caption("Bulk buttons update this draft grid. Nothing is recorded until Save selected reviews is clicked.")
    if select_all or approve_all or reject_all:
        draft = clean(edited.to_dict("records"))
        skipped = 0
        for row in draft:
            if approve_all:
                missing = approval_missing_fields(
                    row["item_id"], row["price"], row["currency"], row["price_basis"], row["offered_quantity"],
                    {"Yes": True, "No": False, "Unknown": None}[row["technical"]],
                )
                row["save"] = not missing
                row["decision"] = "approved" if not missing else "pending"
                skipped += bool(missing)
            else:
                row["save"] = True
                if reject_all:
                    row["decision"] = "rejected"
        st.session_state[draft_key] = draft
        st.session_state[version_key] = st.session_state.get(version_key, 0) + 1
        if approve_all and skipped:
            st.session_state[grid_key + "_notice"] = {
                "kind": "warning", "icon": "⚠️",
                "message": f"Draft updated: {len(draft) - skipped} complete lines marked Approved; {skipped} incomplete lines remain Pending. Nothing has been saved yet.",
            }
        elif approve_all:
            st.session_state[grid_key + "_notice"] = {
                "kind": "info", "icon": "✅",
                "message": f"Draft updated: all {len(draft)} lines marked Approved. Enter a reason and click Save selected reviews to record them.",
            }
        elif reject_all:
            st.session_state[grid_key + "_notice"] = {
                "kind": "info", "icon": "↩️",
                "message": f"Draft updated: all {len(draft)} lines marked Rejected. Enter a reason and click Save selected reviews to record them.",
            }
        else:
            st.session_state[grid_key + "_notice"] = {
                "kind": "info", "icon": "☑️",
                "message": f"Draft updated: all {len(draft)} rows selected. Choose Approved or Rejected, then save.",
            }
        st.rerun()
    if save:
        try:
            selected = [row for row in clean(edited.to_dict("records")) if row["save"]]
            if not selected:
                raise ValueError("Tick Save on at least one reviewed row")
            if any(row["decision"] == "pending" for row in selected):
                raise ValueError("Choose Approved or Rejected for every row you ticked before saving")
            if len(reason.strip()) < 3:
                raise ValueError("Enter a review reason of at least 3 characters")
            for row in selected:
                if row["decision"] == "approved":
                    missing = approval_missing_fields(
                        row["item_id"], row["price"], row["currency"], row["price_basis"], row["offered_quantity"],
                        {"Yes": True, "No": False, "Unknown": None}[row["technical"]],
                    )
                    if missing:
                        raise ValueError(f"Line {row['line']} cannot be approved; missing {', '.join(missing)}. Complete it or choose Rejected.")
            pending = []
            for row in selected:
                value = LineReview(item_id=row["item_id"], price=row["price"], currency=row["currency"], price_basis=row["price_basis"],
                                   offered_quantity=row["offered_quantity"], spec_compliant={"Yes": True, "No": False, "Unknown": None}[row["technical"]],
                                   decision=row["decision"], reason=reason.strip())
                if value.decision == "approved" and value.item_id not in {i.id for i in repo.rfx().items}:
                    raise ValueError("Every approved row needs a valid RFx item")
                pending.append((row["line"] - 1, value.model_dump()))
            for index, value in pending:
                save_line_review(repo, doc["id"], index, value, actor)
            st.session_state.pop(draft_key, None)
            st.session_state[version_key] = st.session_state.get(version_key, 0) + 1
            approved_count = sum(value["decision"] == "approved" for _, value in pending)
            rejected_count = len(pending) - approved_count
            st.session_state[grid_key + "_notice"] = {
                "kind": "success", "icon": "✅",
                "message": f"Saved {len(pending)} line reviews: {approved_count} approved and {rejected_count} rejected. Audit evidence was recorded.",
            }
            st.rerun()
        except Exception as exc:
            show_error(exc)
    candidates = batch_candidates(repo, doc["id"])
    with st.expander(f"Review complete lines together · {len(candidates)} candidates"):
        st.caption("Only unique exact matches with complete fields, source evidence, confidence ≥85% and no extraction issues qualify. Ambiguous and previously reviewed rows stay in individual review.")
        st.write(", ".join(value.item_id for _, value in candidates) or "No candidates")
        with st.form("batch_review_" + doc["id"]):
            checked = st.checkbox("I checked every listed candidate against the original source")
            batch_reason = st.text_input("Batch review reason")
            submit = st.form_submit_button("Approve verified candidate lines", disabled=not candidates)
        if submit:
            try:
                count = approve_verified_batch(repo, doc["id"], batch_reason, actor, checked)
                st.success(f"Approved {count} verified lines. Commercial terms still require a separate review.")
            except Exception as exc:
                show_error(exc)


def manual_line_editor(repo, doc, quote, actor):
    with st.expander("Add a missing line manually"):
        st.caption("Use this when a supplier line was omitted or unreadable during extraction. Enter only values verified against the preserved source or a documented supplier clarification.")
        rfx = repo.rfx()
        with st.form("manual_line_" + doc["id"], clear_on_submit=True):
            a, b = st.columns(2)
            item_id = a.selectbox("RFx item", [item.id for item in rfx.items],
                                  format_func=lambda value: next(f"{item.id} · {item.description}" for item in rfx.items if item.id == value))
            price = b.number_input("Quoted price", min_value=0.0, value=0.0, format="%.4f")
            a, b, c = st.columns(3)
            currency = a.text_input("Currency", value=quote.terms.currency or rfx.base_currency, max_chars=3)
            basis = b.selectbox("Price basis", ["piece", "100_pieces", "box", "100_boxes"])
            requested_qty = next(item.quantity for item in rfx.items if item.id == item_id)
            quantity = c.number_input("Offered quantity", min_value=0, value=requested_qty, step=1)
            technical = st.selectbox("Technical compliance", ["Yes", "No", "Unknown"])
            locator = st.text_input("Source location", placeholder="Example: supplier email dated 25 Sep, paragraph 3")
            excerpt = st.text_area("Source value or clarification", placeholder="Paste the exact supporting wording or describe the verified source value")
            reason = st.text_input("Reason for manual addition", placeholder="Example: OCR missed this row; verified against page 2")
            confirmed = st.checkbox("I verified this line against the source or supplier clarification")
            add = st.form_submit_button("Add and approve manual line", type="primary")
        if add:
            try:
                if not confirmed:
                    raise ValueError("Confirm that you verified the manually entered line")
                if len(locator.strip()) < 2 or len(excerpt.strip()) < 2:
                    raise ValueError("Enter the source location and supporting source value")
                if len(reason.strip()) < 3:
                    raise ValueError("Enter a reason of at least 3 characters")
                currency = currency.strip().upper()
                raw_unit = {"piece": "per piece", "100_pieces": "per 100 pieces", "box": "per box", "100_boxes": "per 100 boxes"}[basis]
                description = next(item.description for item in rfx.items if item.id == item_id)
                line = ExtractedLine(
                    raw_item=item_id, raw_description=description, raw_price=str(price), raw_currency=currency,
                    raw_unit=raw_unit, raw_quantity=str(quantity), raw_spec=technical,
                    matched_item_id=item_id, price=price, currency=currency, price_basis=basis,
                    offered_quantity=quantity, spec_compliant={"Yes": True, "No": False, "Unknown": None}[technical],
                    confidence=1, issues=[], evidence={"locator": locator.strip(), "excerpt": excerpt.strip()},
                )
                review = LineReview(
                    item_id=item_id, price=price, currency=currency, price_basis=basis,
                    offered_quantity=quantity, spec_compliant=line.spec_compliant,
                    decision="approved", reason=reason.strip(),
                )
                index = add_manual_quote_line(repo, doc["id"], line.model_dump(), review.model_dump(), actor)
                st.session_state["review_flash"] = f"Manual line {index + 1} added and approved. Coverage, blockers and comparisons were recalculated."
                st.rerun()
            except Exception as exc:
                show_error(exc)


def review_terms(repo, doc, quote, actor):
    old = repo.reviews(doc["id"]).get("terms")
    terms = old["terms"] if old else quote.terms.model_dump()
    quality = old["quality"] if old else [x.model_dump() for x in quote.quality]
    with st.expander("Original extracted commercial and quality evidence"):
        st.json({"terms": quote.terms.model_dump(), "quality": [x.model_dump() for x in quote.quality], "warnings": quote.warnings})
    with st.form("terms_" + doc["id"]):
        a, b, c = st.columns(3)
        currency = a.text_input("Quote currency", terms["currency"] or "")
        freight = b.text_input("Whole-order freight (quote currency)", str(terms["freight"]) if terms["freight"] is not None else "")
        lead = c.text_input("Delivery lead days", str(terms["lead_days"]) if terms["lead_days"] is not None else "")
        payment = st.text_input("Payment terms", terms["payment_terms"])
        a, b, c = st.columns(3)
        kinds = ["none", "unconditional", "minimum_order", "other", "unknown"]
        kind = a.selectbox("Discount type", kinds, index=kinds.index(terms["discount_kind"]))
        pct = b.text_input("Discount percent", str(terms["discount_percent"]) if terms["discount_percent"] is not None else "")
        threshold = c.text_input("Merchandise threshold (quote currency)", str(terms["discount_threshold"]) if terms["discount_threshold"] is not None else "")
        st.caption("Threshold discounts are automatic only for a sole merchandise-subtotal condition. Advance payment and whole-order conditions require an explicit decision.")
        choices = ["auto", "apply", "ignore"]
        discount_decision = st.selectbox("Discount treatment", choices, index=choices.index(old["discount_decision"] if old else "auto"),
                                        format_func=lambda x: {"auto": "Evaluate supported condition; block if unresolved", "apply": "I verified all conditions are satisfied: apply", "ignore": "Compare without this discount"}[x])
        a, b = st.columns(2)
        tax = a.selectbox("Tax in merchandise AND freight", ["Unknown", "Excluded", "Included"], index={None: 0, False: 1, True: 2}[terms["tax_included"]])
        tax_pct = b.text_input("Tax percent (needed if included)", str(terms["tax_percent"]) if terms["tax_percent"] is not None else "")
        conditions = st.text_area("Other conditions / validity", terms["other_conditions"])
        qrows = []
        for q in repo.rfx().questionnaire:
            matches = [x for x in quality if x["question_id"] == q.id]
            ans = matches[0] if len(matches) == 1 else {"raw_answer": "", "meets_requirement": None, "evidence": {"locator": "", "excerpt": ""}}
            qrows.append({"id": q.id, "question": q.question, "mandatory": q.mandatory, "response": ans["raw_answer"],
                          "meets": {True: "Yes", False: "No", None: "Unknown"}[ans["meets_requirement"]],
                          "source": ans["evidence"]["locator"], "excerpt": ans["evidence"]["excerpt"]})
        st.subheader("Quality responses")
        edited = st.data_editor(frame(qrows), hide_index=True, width="stretch", disabled=["id", "question", "mandatory"],
                                column_config={"meets": st.column_config.SelectboxColumn(options=["Yes", "No", "Unknown"])})
        reason = st.text_input("Commercial and quality review reason", placeholder="Document clarifications, discount decision, and source checks")
        confirm = st.checkbox("I reviewed the source, all conditions and extraction warnings. Unknown fields remain unresolved.")
        save = st.form_submit_button("Approve reviewed terms and responses", type="primary")
    if save:
        try:
            if not confirm:
                raise ValueError("Confirm that you reviewed the commercial and quality evidence")
            if len(reason.strip()) < 3:
                raise ValueError("Enter a commercial and quality review reason of at least 3 characters")
            terms.update(currency=currency.strip().upper() or None, freight=optional_number(freight), lead_days=optional_number(lead, True), payment_terms=payment,
                         discount_kind=kind, discount_percent=optional_number(pct), discount_threshold=optional_number(threshold),
                         tax_included={"Unknown": None, "Excluded": False, "Included": True}[tax], tax_percent=optional_number(tax_pct), other_conditions=conditions)
            answers = [{"question_id": x["id"], "raw_answer": x["response"], "meets_requirement": {"Yes": True, "No": False, "Unknown": None}[x["meets"]],
                        "evidence": {"locator": x["source"], "excerpt": x["excerpt"]}} for x in clean(edited.to_dict("records"))]
            value = TermsReview(terms=terms, quality=answers, discount_decision=discount_decision, decision="approved", reason=reason.strip())
            save_terms_review(repo, doc["id"], value.model_dump(), actor)
            st.session_state["review_flash"] = "Commercial terms and quality responses saved. Blocker counts were recalculated from the live dataset."
            st.rerun()
        except Exception as exc:
            show_error(exc)


def assumptions_editor(repo, actor):
    current = repo.assumptions()
    base = repo.rfx().base_currency
    common = ["USD", "EUR", "GBP", "AED", "SGD", "JPY", "CNY", "AUD", "CAD", "CHF"]
    currencies = list(dict.fromkeys([x.currency for x in current.fx] + [x for x in common if x != base]))
    st.markdown("**1. Convert foreign quotes into the RFx currency**")
    st.write(f"Enter how many **{base}** equal one unit of the supplier's currency. QuoteIQ never fetches or guesses a market rate.")
    st.info(f"Example: Currency **USD**, rate **85** means **1 USD = 85 {base}**. Label a demo rate as fictional in the source field.")
    with st.form("assumptions"):
        cols = ["currency", "rate", "as_of", "source"]
        fx = st.data_editor(pd.DataFrame([x.model_dump() for x in current.fx], columns=cols), num_rows="dynamic", hide_index=True, width="stretch",
                            column_config={
                                "currency": st.column_config.SelectboxColumn("Supplier currency", options=currencies, required=True,
                                    help="Most-used currencies are provided. Do not add the RFx base currency."),
                                "rate": st.column_config.NumberColumn(f"{base} per 1 currency unit", min_value=0.000001, format="%.6f",
                                    help=f"Example: 85 means 1 unit of the selected currency equals 85 {base}."),
                                "as_of": st.column_config.TextColumn("Rate date", help="Use YYYY-MM-DD, for example 2026-09-24"),
                                "source": st.column_config.TextColumn("Rate source / scenario",
                                    help="Example: Buyer-approved fictional demo assumption. Do not present it as a market rate."),
                            })
        st.caption("Use the + row control to add USD, EUR, GBP, AED, SGD, JPY, CNY, AUD, CAD or CHF. Delete a row to remove that assumption.")
        st.divider()
        st.markdown("**2. Resolve supplier 'box' pricing only when its meaning is confirmed**")
        st.caption("Per-piece and per-100-piece prices are converted automatically. For a price per box, record how many RFx requested units are inside one supplier box. Leave it unresolved until the source or supplier confirms the pack size.")
        current_mode = "Unconfirmed" if current.requested_units_per_box is None else ("1 box = 1 requested unit" if current.requested_units_per_box == 1 else "Custom pack size")
        box_mode = st.radio("Box conversion status", ["Unconfirmed", "1 box = 1 requested unit", "Custom pack size"],
                            index=["Unconfirmed", "1 box = 1 requested unit", "Custom pack size"].index(current_mode), horizontal=True)
        default_pack = float(current.requested_units_per_box or 1)
        pack_size = st.number_input("Requested units contained in 1 supplier box (used for Custom pack size)", min_value=0.000001, value=default_pack,
                                    help="Example: enter 25 when a supplier price of INR 500 per box covers 25 requested pieces. This value is ignored unless Custom pack size is selected.")
        if box_mode == "1 box = 1 requested unit":
            st.info("QuoteIQ will divide a per-box price by 1. A price per 100 boxes will be divided by 100.")
        elif box_mode == "Custom pack size":
            st.info(f"QuoteIQ will divide a per-box price by {pack_size:g}. A price per 100 boxes will be divided by {100 * pack_size:g}.")
        else:
            st.warning("Box-priced lines remain unresolved and ineligible for cost comparison until a conversion is confirmed.")
        reason = st.text_input("Source or evidence for this conversion", current.unit_reason,
                               placeholder="Example: Supplier quotation footnote says 25 pieces per box")
        save = st.form_submit_button("Save conversion assumptions")
    if save:
        try:
            rows = [row for row in clean(fx.to_dict("records")) if any(value not in (None, "") for value in row.values())]
            for index, row in enumerate(rows):
                missing = [label for key, label in (("currency", "currency"), ("rate", "rate"), ("as_of", "date"), ("source", "source"))
                           if row.get(key) in (None, "")]
                if missing:
                    raise ValueError(f"FX row {index + 1}: complete the {', '.join(missing)}")
            units_per_box = None if box_mode == "Unconfirmed" else (1 if box_mode == "1 box = 1 requested unit" else pack_size)
            value = Assumptions(fx=rows, requested_units_per_box=units_per_box, unit_reason=reason)
            if any(x.currency == repo.rfx().base_currency for x in value.fx):
                raise ValueError("Base currency already uses FX=1; enter foreign currencies only")
            repo.set_state("assumptions", value.model_dump(), actor, "Buyer supplied conversion assumptions")
            st.session_state["review_flash"] = "Conversion assumptions saved. Blocker counts were recalculated from the live dataset."
            st.rerun()
        except Exception as exc:
            show_error(exc)


def review_desk(repo, snapshot, actor):
    st.title("Review desk")
    st.caption("Every approval has a person, a reason and the original evidence behind it.")
    flash = st.session_state.pop("review_flash", None)
    if flash:
        st.success(flash)
        st.toast(flash, icon="✅")
    if not repo.rfx():
        st.info("Create an RFx first.")
        return
    with st.expander("Currency and unit assumptions", expanded=repo.assumptions().requested_units_per_box is None):
        assumptions_editor(repo, actor)
    docs = [d for d in repo.documents() if d["extraction"]]
    if not docs:
        st.info("Extract a quote in Quote inbox to start reviewing.")
        return
    active_docs = [d for d in docs if d["active"] and d["rfx_hash"] == repo.rfx_hash()]
    guided_result = st.session_state.pop("guided_review_flash", None)
    if guided_result:
        st.success(f"Guided review saved: {guided_result['approved_lines']} complete lines and {guided_result['approved_terms']} commercial/quality reviews approved. {len(guided_result['exceptions'])} line exception(s) remain for individual correction.")
        if guided_result["exceptions"]:
            st.dataframe(frame(guided_result["exceptions"]), hide_index=True, width="stretch")
    if st.session_state.get("experience", "Guided demo") == "Guided demo" and active_docs:
        with st.expander("Presentation fast track", expanded=True):
            st.write("Verify the clean extracted values once, then keep only genuinely missing or ambiguous rows for individual correction. This action does not generate or replace any supplier value.")
            with st.form("guided_demo_review"):
                guided_reason = st.text_input("Guided review reason", value="Verified complete demo values against the preserved originals")
                guided_confirmed = st.checkbox("I checked the proposed complete values against the demo originals")
                run_guided = st.form_submit_button("Approve complete demo values and isolate exceptions", type="primary")
            if run_guided:
                try:
                    result = guided_demo_review(repo, actor, guided_reason, guided_confirmed)
                    st.session_state["guided_review_flash"] = result
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
    with st.expander(f"Review multiple sources together · {len(active_docs)} active sources"):
        st.warning("Bulk approval means you checked every selected source, line, commercial term and quality response. Missing line facts cannot be approved.")
        with st.form("bulk_source_review"):
            source_ids = st.multiselect(
                "Sources", [d["id"] for d in active_docs], default=[d["id"] for d in active_docs],
                format_func=lambda x: next(f"{d['supplier_id']} · {d['filename']}" for d in active_docs if d["id"] == x),
            )
            bulk_reason = st.text_input("Bulk source review reason", placeholder="Explain what you checked across the selected originals")
            bulk_confirm = st.checkbox("I reviewed every selected original, all line values, commercial conditions, quality responses and warnings")
            approve_sources, reject_sources = st.columns(2)
            approve_all_sources = approve_sources.form_submit_button("Approve selected sources", type="primary")
            reject_all_sources = reject_sources.form_submit_button("Reject selected sources")
        if approve_all_sources or reject_all_sources:
            try:
                result = review_sources_bulk(repo, source_ids, "approved" if approve_all_sources else "rejected",
                                             bulk_reason, actor, bulk_confirm)
                st.success(f"Reviewed {result['sources']} sources and {result['lines']} lines.")
                st.rerun()
            except Exception as exc:
                show_error(exc)
    selected = st.selectbox("Source to review", [d["id"] for d in docs], format_func=lambda x: next(f"{d['supplier_id']} · {d['filename']} · {d['created_at'][:19]}{' · ACTIVE' if d['active'] else ''}" for d in docs if d["id"] == x))
    doc = repo.document(selected)
    if doc["rfx_hash"] != repo.rfx_hash():
        st.warning("This source belongs to an earlier RFx revision. Its reviews cannot be applied to the current RFx; re-upload it.")
        source_view(doc)
        return
    if not doc["active"] and st.button("Use this quote revision for comparison"):
        repo.activate_document(doc["id"], actor)
        st.rerun()
    quote = ExtractedQuote.model_validate_json(doc["extraction"])
    for warning in quote.warnings:
        st.warning(warning)
    section = st.radio(
        "Review section", ["Line items", "Commercial & quality", "Original source", "Raw extraction"],
        horizontal=True, key="review_section_" + doc["id"],
    )
    if section == "Line items":
        manual_line_editor(repo, doc, quote, actor)
        review_lines(repo, doc, quote, actor)
    elif section == "Commercial & quality":
        review_terms(repo, doc, quote, actor)
    elif section == "Original source":
        source_view(doc)
    else:
        st.json(quote.model_dump())
        st.json(json.loads(doc["metadata"] or "{}"))


def inline_blocker_resolver(repo, snapshot, actor):
    suppliers = list(snapshot.get("suppliers", []))
    if not suppliers:
        return
    suppliers.sort(key=lambda summary: not bool(summary.get("blockers") or summary.get("eligibility_reasons")))
    with st.expander("Resolve blockers here", expanded=False):
        st.caption("Edit the evidence-backed value and save it here. QuoteIQ recalculates the comparison immediately and records the correction in the audit log. Critical blockers cannot be dismissed without resolving their underlying data.")
        supplier_id = st.selectbox(
            "Supplier to review", [summary["supplier_id"] for summary in suppliers],
            format_func=lambda value: next(summary["supplier"] for summary in suppliers if summary["supplier_id"] == value),
            key="compare_resolve_supplier",
        )
        supplier_summary = next(summary for summary in suppliers if summary["supplier_id"] == supplier_id)
        supplier_rows = [row for row in blocker_table(snapshot) if row["Supplier"] == supplier_summary["supplier"]]
        if supplier_rows:
            st.dataframe(frame(supplier_rows), hide_index=True, width="stretch")
        else:
            st.success("This supplier currently has no rule-based blockers or eligibility exclusions.")
        eligibility_rows = [row for row in supplier_rows if row["Type"] == "Eligibility"]
        if eligibility_rows:
            st.markdown("**What these eligibility exclusions mean**")
            for row in eligibility_rows:
                st.info(f"**{row['Issue']}**\n\n{row['Details']}")
        exclusions = repo.get_state("supplier_exclusions", {})
        current_exclusion = exclusions.get(supplier_id, {})
        with st.form("supplier_eligibility_decision_" + supplier_id):
            st.markdown("**Buyer eligibility decision**")
            st.caption("Exclude a supplier for a documented procurement reason. This does not change extracted data or override other eligibility rules.")
            eligibility_decision = st.radio(
                "Decision", ["Use rule-based eligibility", "Exclude supplier"], horizontal=True,
                index=1 if current_exclusion.get("excluded") else 0,
            )
            exclusion_reason = st.text_input(
                "Eligibility decision reason", value=current_exclusion.get("reason", ""),
                placeholder="Example: Supplier cannot meet the mandatory delivery date",
            )
            save_eligibility = st.form_submit_button("Save eligibility decision")
        if save_eligibility:
            try:
                updated = dict(exclusions)
                if eligibility_decision == "Exclude supplier":
                    if len(exclusion_reason.strip()) < 3:
                        raise ValueError("Enter a reason of at least 3 characters before excluding the supplier")
                    updated[supplier_id] = {"excluded": True, "reason": exclusion_reason.strip(), "actor": actor, "recorded_at": now()}
                    message = f"{supplier_summary['supplier']} excluded from eligible comparisons."
                else:
                    updated.pop(supplier_id, None)
                    message = f"{supplier_summary['supplier']} restored to rule-based eligibility evaluation."
                repo.set_state("supplier_exclusions", updated, actor, exclusion_reason.strip() or "Buyer restored rule-based eligibility")
                st.session_state["compare_eligibility_flash"] = message
                st.rerun()
            except Exception as exc:
                show_error(exc)
        section = st.radio(
            "Correction area", ["Line items", "Commercial & quality", "Currency and unit assumptions", "Original source"],
            horizontal=True, key="compare_resolve_section",
        )
        if section == "Currency and unit assumptions":
            assumptions_editor(repo, actor)
            return
        docs = [doc for doc in repo.documents(active_only=True)
                if doc["supplier_id"] == supplier_id and doc["extraction"] and doc["rfx_hash"] == repo.rfx_hash()]
        if not docs:
            st.info("No active extracted quote is available for this supplier. Upload or extract its source in Quote inbox.")
            return
        doc = docs[0]
        quote = ExtractedQuote.model_validate_json(doc["extraction"])
        st.caption(f"Editing {doc['filename']}")
        if section == "Line items":
            affected_indexes = {line["line_index"] for line in snapshot.get("lines", [])
                                if line.get("supplier_id") == supplier_id
                                and (line.get("issues") or line.get("review_status") != "approved")}
            review_lines(repo, doc, quote, actor, focus_indexes=affected_indexes, highlight_attention=True)
        elif section == "Commercial & quality":
            review_terms(repo, doc, quote, actor)
        else:
            source_view(doc)


def compare_page(repo, snapshot, actor):
    st.title("Compare & decide")
    st.caption(snapshot.get("cost_basis", ""))
    eligibility_flash = st.session_state.pop("compare_eligibility_flash", None)
    if eligibility_flash:
        st.success(eligibility_flash)
    if not repo.rfx():
        st.info("Create an RFx first.")
        return
    if snapshot.get("extracted_supplier_count", 0) == 0:
        st.info(snapshot["decision"])
    elif snapshot.get("critical_warnings", snapshot["warnings"]):
        st.warning(snapshot["decision"])
        st.caption("To reach a decision-ready result, resolve each active critical issue or explicitly exclude the affected supplier with a reason.")
    else:
        st.success(snapshot["decision"])
    live_excluded_ids = {supplier_id for supplier_id, decision in repo.get_state("supplier_exclusions", {}).items()
                         if decision.get("excluded")}
    show_excluded = st.toggle("Show manually excluded suppliers", value=False,
                              help="Excluded suppliers remain available for audit and can be restored in Resolve blockers here.")
    active_supplier_summaries = [summary for summary in snapshot["suppliers"] if summary["supplier_id"] not in live_excluded_ids]
    visible_supplier_summaries = snapshot["suppliers"] if show_excluded else active_supplier_summaries
    visible_supplier_ids = {summary["supplier_id"] for summary in visible_supplier_summaries}
    visible_suppliers = [supplier for supplier in repo.suppliers() if supplier.id in visible_supplier_ids]
    hidden_names = [summary["supplier"] for summary in snapshot["suppliers"] if summary["supplier_id"] in live_excluded_ids]
    if hidden_names and not show_excluded:
        st.info("Excluded from active comparison: " + ", ".join(hidden_names))
    elif hidden_names:
        st.info("Shown for audit only and still excluded from headline calculations: " + ", ".join(hidden_names))
    displayed_lowest = lowest_visible(active_supplier_summaries)
    displayed_eligible = lowest_visible(active_supplier_summaries, eligible_only=True)
    a, b, c = st.columns(3)
    def amount(result):
        return f"{snapshot['base_currency']} {result['total']:,.2f}" if result else "Unavailable"
    a.metric("Lowest complete reviewed total", amount(displayed_lowest))
    b.metric("Lowest eligible complete total", amount(displayed_eligible))
    c.metric("Eligible supplier bids", sum(bool(s["eligible"]) for s in active_supplier_summaries))
    for label, value in [("Lowest total", displayed_lowest), ("Lowest eligible total", displayed_eligible)]:
        if value:
            st.caption(f"{label}: {', '.join(value['suppliers'])}. Scope: complete reviewed quotes with known commercial costs.")
    st.markdown("#### Actions and exceptions")
    show_blockers(snapshot, expanded=False)
    inline_blocker_resolver(repo, snapshot, actor)
    tabs = st.tabs(["30 × 5 comparison", "Delivered cost", "Quality", "Cheapest eligible lines", "Evidence & blockers"])
    with tabs[0]:
        eligible = st.toggle("Show eligible prices only", value=True)
        table = []
        for item in repo.rfx().items:
            record = {"Item": item.id, "Description": item.description, "Quantity": item.quantity}
            for supplier in visible_suppliers:
                matches = [x for x in snapshot["lines"] if x["item_id"] == item.id and x["supplier_id"] == supplier.id and x["review_status"] != "rejected"]
                if len(matches) > 1:
                    value = "Duplicate — review"
                elif not matches:
                    value = "Not quoted"
                else:
                    row = matches[0]
                    if row["unit_price"] is None:
                        value = "Unresolved"
                    elif eligible and not row["eligible"]:
                        value = "Not eligible / unreviewed"
                    else:
                        value = f"{row['unit_price']:,.4f}" + (" · review" if row["issues"] else "")
                record[supplier.name] = value
            table.append(record)
        st.caption(f"{snapshot['base_currency']} per individual piece. Missing prices are never shown as zero. Open Evidence & blockers for source references.")
        st.dataframe(frame(table), hide_index=True, width="stretch", height=650)
        st.download_button("Export comparison CSV", csv_bytes(table), "QuoteIQ-matrix.csv")
    with tabs[1]:
        columns = ["supplier", "coverage", "reviewed", "subtotal", "discount", "freight", "total", "eligible", "lead_days", "payment_terms"]
        st.dataframe(frame(visible_supplier_summaries)[columns], hide_index=True, width="stretch")
        st.caption("Subtotal can be partial or unreviewed. Only nonempty Total cells are complete reviewed comparisons. Totals exclude tax; tax-inclusive source amounts are stripped only when their tax rate is known.")
        known = [s for s in visible_supplier_summaries if s["total"] is not None]
        if known:
            st.bar_chart(frame(known).set_index("supplier")[["total"]], color="#278765")
        for s in visible_supplier_summaries:
            with st.expander(s["supplier"] + " · cost evidence"):
                st.json(s)
    with tabs[2]:
        quality_matrix = []
        for question in repo.rfx().questionnaire:
            record = {"Question": question.question, "Mandatory": question.mandatory}
            for supplier in visible_suppliers:
                answer = next((q for q in snapshot["quality"] if q["supplier"] == supplier.name and q["question_id"] == question.id), None)
                record[supplier.name] = ("Not provided" if answer is None else
                                         {True: "Meets", False: "Does not meet", None: "Unknown"}[answer["meets_requirement"]]
                                         + (" · unreviewed" if not answer["reviewed"] else ""))
            quality_matrix.append(record)
        st.dataframe(frame(quality_matrix), hide_index=True, width="stretch")
        with st.expander("Quality responses and source evidence"):
            st.dataframe(frame(snapshot["quality"]), hide_index=True, width="stretch")
    with tabs[3]:
        st.info("Material price comparison only. Order freight, volume discounts, minimum orders and split-award economics require separate confirmation. This table is not an award recommendation.")
        st.dataframe(frame(snapshot["cheapest_lines"]), hide_index=True, width="stretch", height=600)
    with tabs[4]:
        if snapshot["warnings"]:
            st.dataframe(frame([{"unresolved": x} for x in snapshot["warnings"]]), hide_index=True, width="stretch")
        st.dataframe(frame(snapshot["lines"]), hide_index=True, width="stretch")
        st.json(snapshot["assumptions"])
    st.download_button("Download comparison + calculation evidence", comparison_workbook(snapshot, repo.audit()), "QuoteIQ-comparison.xlsx", type="primary")
    if st.button("Prepare complete evidence bundle"):
        st.session_state["bundle"] = (snapshot["dataset_version"], evidence_zip(repo, snapshot))
    if st.session_state.get("bundle", (None,))[0] == snapshot["dataset_version"]:
        st.download_button("Download originals, RFx, audit and comparison", st.session_state["bundle"][1], "QuoteIQ-evidence.zip")


def compare_page_clean(repo, snapshot, actor):
    """Buyer-first comparison with audit detail progressively disclosed."""
    st.title("Compare & decide")
    st.caption("Reviewed delivered costs, eligibility and source evidence in one decision workspace.")
    if not repo.rfx():
        st.info("Create an RFx first.")
        return
    flash = st.session_state.pop("compare_eligibility_flash", None)
    if flash:
        st.success(flash)

    live_excluded_ids = {supplier_id for supplier_id, decision in repo.get_state("supplier_exclusions", {}).items()
                         if decision.get("excluded")}
    active = [summary for summary in snapshot["suppliers"] if summary["supplier_id"] not in live_excluded_ids]
    lowest_total = lowest_visible(active)
    lowest_eligible = lowest_visible(active, eligible_only=True)
    def amount(result):
        return f"{snapshot['base_currency']} {result['total']:,.2f}" if result else "Unavailable"

    if snapshot.get("extracted_supplier_count", 0) == 0:
        st.info(snapshot["decision"])
    elif snapshot.get("critical_warnings", snapshot["warnings"]):
        st.warning(snapshot["decision"])
        st.caption("To reach a decision-ready result, resolve each active critical issue or explicitly exclude the affected supplier with a reason.")
    else:
        st.success(snapshot["decision"])
    a, b, c = st.columns(3)
    a.metric("Lowest reviewed total", amount(lowest_total))
    b.metric("Lowest eligible total", amount(lowest_eligible))
    c.metric("Eligible bids", sum(bool(summary["eligible"]) for summary in active))
    if lowest_eligible:
        st.caption(f"Current lowest eligible bid: {', '.join(lowest_eligible['suppliers'])}. QuoteIQ presents evidence; the buyer makes the award decision.")

    hidden_names = [summary["supplier"] for summary in snapshot["suppliers"] if summary["supplier_id"] in live_excluded_ids]
    show_excluded = st.toggle("Include manually excluded suppliers for audit", value=False)
    visible_summaries = snapshot["suppliers"] if show_excluded else active
    visible_ids = {summary["supplier_id"] for summary in visible_summaries}
    visible_suppliers = [supplier for supplier in repo.suppliers() if supplier.id in visible_ids]
    if hidden_names:
        message = ("Shown for audit only; excluded from calculations: " if show_excluded else "Excluded from active comparison: ")
        st.info(message + ", ".join(hidden_names))

    st.markdown(f"#### Issues and corrections · {len(snapshot.get('critical_warnings', []))} critical")
    show_blockers(snapshot, expanded=False)
    inline_blocker_resolver(repo, snapshot, actor)

    summary_tab, items_tab, quality_tab, evidence_tab = st.tabs(
        ["Decision summary", "Item prices", "Quality", "Evidence & exports"])
    with summary_tab:
        ranking = []
        for summary in sorted(visible_summaries, key=lambda value: (value["total"] is None, value["total"] or 0)):
            status = ("Excluded" if summary["supplier_id"] in live_excluded_ids else "Eligible" if summary["eligible"]
                      else "Needs review" if summary["total"] is None else "Ineligible")
            ranking.append({"Supplier": summary["supplier"], "Status": status,
                            "Coverage": f"{summary['coverage']}/{len(repo.rfx().items)}",
                            "Reviewed": summary["reviewed"], "Delivered total": summary["total"],
                            "Lead days": summary["lead_days"], "Payment": summary["payment_terms"]})
        st.dataframe(frame(ranking), hide_index=True, width="stretch",
                     column_config={"Delivered total": st.column_config.NumberColumn(format=f"{snapshot['base_currency']} %.2f")})
        st.caption("A delivered total appears only when the quote is complete, reviewed and commercially calculable.")
        chart_rows = [summary for summary in active if summary["total"] is not None]
        if chart_rows:
            st.bar_chart(frame(chart_rows).set_index("supplier")[["total"]], color="#278765")
        with st.expander("Inspect one supplier calculation"):
            selected = st.selectbox("Supplier", [summary["supplier_id"] for summary in visible_summaries],
                                    format_func=lambda value: next(summary["supplier"] for summary in visible_summaries if summary["supplier_id"] == value),
                                    key="clean_comparison_supplier")
            detail = next(summary for summary in visible_summaries if summary["supplier_id"] == selected)
            fields = ["supplier", "coverage", "reviewed", "subtotal", "discount", "freight", "total", "eligible",
                      "lead_days", "payment_terms", "discount_evidence", "formula", "blockers", "eligibility_reasons"]
            st.json({field: detail.get(field) for field in fields})

    with items_tab:
        item_view = st.radio("View", ["Side-by-side prices", "Cheapest eligible by line"], horizontal=True)
        if item_view == "Side-by-side prices":
            eligible_only = st.toggle("Eligible prices only", value=True)
            matrix = []
            for item in repo.rfx().items:
                record = {"Item": item.id, "Description": item.description, "Quantity": item.quantity}
                for supplier in visible_suppliers:
                    matches = [line for line in snapshot["lines"] if line["item_id"] == item.id
                               and line["supplier_id"] == supplier.id and line["review_status"] != "rejected"]
                    if len(matches) > 1:
                        value = "Duplicate - review"
                    elif not matches:
                        value = "Not quoted"
                    elif matches[0]["unit_price"] is None:
                        value = "Unresolved"
                    elif eligible_only and not matches[0]["eligible"]:
                        value = "Not eligible"
                    else:
                        value = f"{matches[0]['unit_price']:,.4f}" + (" - review" if matches[0]["issues"] else "")
                    record[supplier.name] = value
                matrix.append(record)
            st.caption(f"{snapshot['base_currency']} per requested unit. Missing values remain missing and are never displayed as zero.")
            st.dataframe(frame(matrix), hide_index=True, width="stretch", height=540)
            st.download_button("Export item prices", csv_bytes(matrix), "QuoteIQ-item-prices.csv")
        else:
            st.info("Material prices only. Order freight and discounts are not allocated to lines, so this is not a split-award recommendation.")
            st.dataframe(frame(snapshot["cheapest_lines"]), hide_index=True, width="stretch", height=540)

    with quality_tab:
        quality_matrix = []
        for question in repo.rfx().questionnaire:
            record = {"Question": question.question, "Mandatory": question.mandatory}
            for supplier in visible_suppliers:
                answer = next((value for value in snapshot["quality"] if value["supplier"] == supplier.name
                               and value["question_id"] == question.id), None)
                record[supplier.name] = ("Not provided" if answer is None else
                                         {True: "Meets", False: "Does not meet", None: "Unknown"}[answer["meets_requirement"]]
                                         + (" - unreviewed" if not answer["reviewed"] else ""))
            quality_matrix.append(record)
        st.dataframe(frame(quality_matrix), hide_index=True, width="stretch")
        with st.expander("Source evidence for quality responses"):
            names = {supplier.name for supplier in visible_suppliers}
            st.dataframe(frame([row for row in snapshot["quality"] if row["supplier"] in names]), hide_index=True, width="stretch")

    with evidence_tab:
        if snapshot["warnings"]:
            st.dataframe(frame([{"Active unresolved issue": value} for value in snapshot["warnings"]]), hide_index=True, width="stretch")
        else:
            st.success("No active comparison blockers remain.")
        with st.expander("Normalized line evidence"):
            st.dataframe(frame(snapshot["lines"]), hide_index=True, width="stretch", height=450)
        with st.expander("Conversion assumptions"):
            st.json(snapshot["assumptions"])
        st.download_button("Download comparison workbook", comparison_workbook(snapshot, repo.audit()),
                           "QuoteIQ-comparison.xlsx", type="primary")
        if st.button("Prepare full evidence bundle"):
            st.session_state["bundle"] = (snapshot["dataset_version"], evidence_zip(repo, snapshot))
        if st.session_state.get("bundle", (None,))[0] == snapshot["dataset_version"]:
            st.download_button("Download originals, RFx, audit and comparison", st.session_state["bundle"][1], "QuoteIQ-evidence.zip")


def analyst_page(repo, snapshot, actor):
    st.title("Ask your quote analyst")
    st.caption("Answers grounded in this workspace, with validated tools and reproducible calculation evidence.")
    st.info("Try: Which supplier has the lowest complete delivered cost? · Why is MetroBox ineligible? · Compare PKG-012 prices and show the original source. · Which mandatory quality answers are missing?")
    if not repo.rfx():
        st.info("Create an RFx and extract supplier quotes first.")
        return
    question = st.text_area("Your question", placeholder="What is preventing an award decision?", height=100)
    if st.button("Ask analyst", type="primary", disabled=not llm.configured() or not question.strip()):
        try:
            with st.spinner("Checking the quote data and calculating supporting evidence…"):
                result = analyst.ask(question, snapshot, repo.rfx())
                result["question"] = question
                repo.log("analyst_query", snapshot["dataset_version"], after=result, actor=actor, reason="Read-only tool-grounded analysis")
                st.session_state["analyst_result"] = result
        except Exception as exc:
            show_error(exc)
    result = st.session_state.get("analyst_result")
    if result:
        if result["dataset_version"] != snapshot["dataset_version"]:
            st.warning("This answer refers to an earlier dataset. Ask again after your changes.")
        st.caption(result["question"])
        st.markdown(result["answer"])
        st.caption("AI interpretation can be mistaken. The deterministic evidence below is the basis for review.")
        for evidence in result["evidence"]:
            with st.expander(f"[{evidence['evidence_id']}] {evidence['operation']} · calculation evidence", expanded=True):
                st.write(evidence["method"])
                records = evidence["records"]
                if records:
                    st.dataframe(frame(records), hide_index=True, width="stretch")
                    st.download_button("Export support table", csv_bytes(records), f"QuoteIQ-{evidence['evidence_id']}.csv", key="export_" + evidence["evidence_id"])
                    if evidence["operation"] == "supplier_totals":
                        plotted = [r for r in records if r["total"] is not None]
                        if plotted:
                            st.bar_chart(frame(plotted).set_index("supplier")[["total"]])
                st.json({k: evidence[k] for k in ("scope", "assumptions", "unresolved_assumptions", "dataset_version")})


def audit_page(repo, snapshot, actor):
    st.title("Audit trail")
    st.caption("Original values, normalized snapshots, human corrections and analyst evidence. Stored in an application-enforced append-only log.")
    audit = repo.audit()
    if not audit:
        st.info("Your workspace actions will appear here.")
        return
    actions = st.multiselect("Filter actions", sorted({x["action"] for x in audit}))
    records = [x for x in audit if not actions or x["action"] in actions]
    st.dataframe(frame(records)[["id", "timestamp", "actor", "action", "entity", "reason"]], hide_index=True, width="stretch", height=400)
    if records:
        chosen = st.selectbox("Inspect an event", [r["id"] for r in records], format_func=lambda x: next(f"#{r['id']} · {r['action']} · {r['entity'][:35]}" for r in records if r["id"] == x))
        event = next(x for x in records if x["id"] == chosen)
        a, b = st.columns(2)
        with a:
            st.write("Before / original")
            render_audit_value(event["before_json"])
        with b:
            st.write("After / reviewed")
            render_audit_value(event["after_json"])
    st.download_button("Export complete audit log", json.dumps(audit, indent=2, ensure_ascii=False), "QuoteIQ-audit.json")


def decode_audit_value(raw):
    """Decode stored audit JSON while tolerating legacy/plain-text records."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def render_audit_value(raw):
    """Render objects as JSON and scalar values as text for Streamlit."""
    value = decode_audit_value(raw)
    if isinstance(value, (dict, list)):
        st.json(value)
    elif value is None:
        st.caption("No prior value")
    else:
        st.code(str(value), language=None, wrap_lines=True)


def main():
    st.set_page_config(page_title="QuoteIQ · Sourcing workspace", page_icon="📦", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    with st.sidebar:
        st.markdown('<div class="brand">Quote<span>IQ</span></div><div class="subbrand">PROCUREMENT, WITH PROOF</div>', unsafe_allow_html=True)
        experience = st.selectbox("Experience", ["Guided demo", "Full workspace"], key="experience",
                                  help="Guided demo keeps the assignment dataset and presentation shortcuts. Full workspace uses separate data and removes demo size restrictions.")
        st.caption("Presentation workflow · five fictional suppliers" if experience == "Guided demo" else
                   "Separate unrestricted workspace · add your own suppliers and RFx rows")
        st.divider()
    base_root = Path(os.getenv("QUOTEIQ_DATA_DIR", "data"))
    repo = Repository(base_root if experience == "Guided demo" else base_root / "full_workspace")
    if experience == "Guided demo" and not repo.suppliers():
        repo.set_state("suppliers", [s.model_dump() for s in demo_data.demo_suppliers()], actor="system", reason="Five editable fictional suppliers")
    snapshot = build_snapshot(repo)
    with st.sidebar:
        page = st.radio("Workspace", PAGES, label_visibility="collapsed", key="page")
        st.divider()
        actor = st.text_input("Reviewer name", value="Buyer", help="Recorded with edits and approvals. This demo does not authenticate users.")
        st.caption(f"AI provider: {llm.provider_name()}" if llm.configured() else "AI key not configured")
        try:
            st.caption(f"Model: {llm.model_name()}")
        except llm.AIError:
            st.caption("Model: no provider configured")
        st.caption("Guided assignment workspace" if experience == "Guided demo" else "Full sourcing workspace")
        critical_count = sum(row["Type"] == "Critical blocker" for row in blocker_table(snapshot))
        eligible_count = sum(bool(s.get("eligible")) for s in snapshot.get("suppliers", []))
        st.metric("Critical blockers", critical_count)
        st.caption(f"{eligible_count} complete eligible bid(s) · recalculated from saved live data")
        st.caption("QuoteIQ / Aerchain Builder")
    try:
        handlers = [overview, rfx_studio, suppliers_page, inbox, review_desk, compare_page_clean, analyst_page, audit_page]
        handlers[PAGES.index(page)](repo, snapshot, actor)
    except Exception as exc:
        show_error(exc)
        st.caption("The operation did not complete. Original source files and earlier audit records are retained.")
