"""Shareable evidence exports with spreadsheet formula injection protection."""
import io
import json
from zipfile import ZIP_DEFLATED, ZipFile
import pandas as pd


def safe_cell(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def frame(records):
    return pd.DataFrame(records).map(safe_cell)


def csv_bytes(records):
    return frame(records).to_csv(index=False).encode("utf-8-sig")


def comparison_workbook(snapshot, audit):
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        for label, records in [("Supplier totals", snapshot["suppliers"]), ("Normalized lines", snapshot["lines"]),
                               ("Quality", snapshot["quality"]), ("Cheapest eligible lines", snapshot["cheapest_lines"]),
                               ("Audit", audit), ("Assumptions", [snapshot["assumptions"]]),
                               ("Read me", [{"basis": snapshot["cost_basis"], "decision": snapshot["decision"], "version": snapshot["dataset_version"]}])]:
            frame(records).to_excel(writer, sheet_name=label, index=False)
            sheet = writer.sheets[label]
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
    return out.getvalue()


def rfx_workbook(rfx):
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        frame([x.model_dump() for x in rfx.items]).to_excel(writer, sheet_name="RFx items", index=False)
        frame([x.model_dump() for x in rfx.questionnaire]).to_excel(writer, sheet_name="Quality questions", index=False)
        frame([{k: v for k, v in rfx.model_dump().items() if k not in {"items", "questionnaire"}}]).to_excel(writer, sheet_name="Terms", index=False)
    return out.getvalue()


def evidence_zip(repo, snapshot):
    from pathlib import Path
    out = io.BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as archive:
        archive.writestr("comparison.xlsx", comparison_workbook(snapshot, repo.audit()))
        archive.writestr("snapshot.json", json.dumps(snapshot, indent=2, ensure_ascii=False))
        archive.writestr("audit.json", json.dumps(repo.audit(), indent=2, ensure_ascii=False))
        archive.writestr("rfx.json", repo.rfx().model_dump_json(indent=2))
        archive.writestr("suppliers.json", json.dumps([s.model_dump() for s in repo.suppliers()], indent=2))
        for doc in repo.documents():
            prefix = f"sources/{doc['id']}/"
            archive.write(Path(doc["path"]), prefix + doc["filename"])
            archive.writestr(prefix + "metadata.json", json.dumps(doc, indent=2))
            archive.writestr(prefix + "reviews.json", json.dumps(repo.reviews(doc["id"]), indent=2))
    return out.getvalue()
