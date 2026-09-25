"""SQLite persistence, immutable originals and append-only application audit trail."""
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from .models import Assumptions, RFx, Supplier


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)


class Repository:
    def __init__(self, root=None):
        self.root = Path(root or os.getenv("QUOTEIQ_DATA_DIR", "data"))
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "sources").mkdir(exist_ok=True)
        self.path = self.root / "quoteiq.sqlite3"
        with self.connection() as con:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, supplier_id TEXT NOT NULL, filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL, path TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, rfx_hash TEXT NOT NULL, parsed TEXT,
                    extraction TEXT, error TEXT, metadata TEXT, active INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS reviews (
                    document_id TEXT NOT NULL, target TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(document_id, target));
                CREATE TABLE IF NOT EXISTS manual_lines (
                    document_id TEXT NOT NULL, line_index INTEGER NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(document_id, line_index));
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                    actor TEXT NOT NULL, action TEXT NOT NULL, entity TEXT NOT NULL,
                    before_json TEXT, after_json TEXT, reason TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
                    BEGIN SELECT RAISE(ABORT, 'Audit records are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
                    BEGIN SELECT RAISE(ABORT, 'Audit records are append-only'); END;
            """)

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def _audit(self, con, action, entity, before, after, reason, actor):
        con.execute("INSERT INTO audit(timestamp,actor,action,entity,before_json,after_json,reason) VALUES(?,?,?,?,?,?,?)",
                    (now(), actor, action, entity, dumps(before), dumps(after), reason,))

    def log(self, action, entity, before=None, after=None, reason="", actor="system"):
        with self.connection() as con:
            self._audit(con, action, entity, before, after, reason, actor)

    def get_state(self, key, default=None):
        with self.connection() as con:
            row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key, value, actor="buyer", reason="Saved in workspace"):
        with self.connection() as con:
            previous = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            con.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dumps(value)))
            self._audit(con, "save", key, json.loads(previous[0]) if previous else None, value, reason, actor)

    def rfx(self):
        value = self.get_state("rfx")
        return RFx.model_validate(value) if value else None

    def rfx_hash(self):
        return hashlib.sha256(dumps(self.get_state("rfx")).encode()).hexdigest()

    def suppliers(self):
        return [Supplier.model_validate(x) for x in self.get_state("suppliers", [])]

    def assumptions(self):
        return Assumptions.model_validate(self.get_state("assumptions", {}))

    def add_document(self, supplier_id, filename, data):
        if self.rfx() is None:
            raise ValueError("Create an RFx first")
        if supplier_id not in {s.id for s in self.suppliers()}:
            raise ValueError("Choose a registered supplier")
        doc_id = uuid4().hex
        filename = Path(filename.replace("\\", "/")).name
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / "sources" / (doc_id + Path(filename).suffix.lower())
        path.write_bytes(data)
        with self.connection() as con:
            con.execute("INSERT INTO documents(id,supplier_id,filename,sha256,path,status,created_at,rfx_hash) VALUES(?,?,?,?,?,?,?,?)",
                        (doc_id, supplier_id, filename, digest, str(path.resolve()), "stored", now(), self.rfx_hash()))
            self._audit(con, "upload", doc_id, None, {"filename": filename, "sha256": digest}, "Original source preserved", "buyer")
        return doc_id

    def documents(self, active_only=False):
        with self.connection() as con:
            query = "SELECT * FROM documents" + (" WHERE active=1" if active_only else "") + " ORDER BY created_at"
            return [dict(r) for r in con.execute(query)]

    def document(self, doc_id):
        with self.connection() as con:
            row = con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown document")
        return dict(row)

    def save_parsed(self, doc_id, parsed):
        with self.connection() as con:
            con.execute("UPDATE documents SET parsed=?, status='parsed' WHERE id=?", (dumps(parsed), doc_id))

    def fail_document(self, doc_id, error):
        with self.connection() as con:
            con.execute("UPDATE documents SET status='failed',error=? WHERE id=?", (error, doc_id))
            self._audit(con, "extraction_failed", doc_id, None, {"error": error}, "No substitute values generated", "system")

    def finish_document(self, doc_id, extraction, metadata):
        doc = self.document(doc_id)
        if doc["extraction"]:
            raise ValueError("Extraction is immutable. Upload a new revision to re-extract.")
        with self.connection() as con:
            con.execute("UPDATE documents SET active=0 WHERE supplier_id=?", (doc["supplier_id"],))
            con.execute("UPDATE documents SET extraction=?, metadata=?, status='extracted', error=NULL,active=1 WHERE id=?",
                        (dumps(extraction), dumps(metadata), doc_id))
            provider = (json.loads(metadata).get("provider") if isinstance(metadata, str) else metadata.get("provider")) or "AI"
            self._audit(con, "extraction", doc_id, None, extraction, f"Real {provider} structured extraction; pending human review", provider)

    def activate_document(self, doc_id, actor):
        doc = self.document(doc_id)
        if not doc["extraction"] or doc["rfx_hash"] != self.rfx_hash():
            raise ValueError("Only an extracted quote for the current RFx can be activated")
        with self.connection() as con:
            con.execute("UPDATE documents SET active=0 WHERE supplier_id=?", (doc["supplier_id"],))
            con.execute("UPDATE documents SET active=1 WHERE id=?", (doc_id,))
            self._audit(con, "activate_revision", doc_id, None, {"active": True}, "Buyer selected quote revision", actor)

    def reviews(self, doc_id):
        with self.connection() as con:
            return {r["target"]: json.loads(r["data"]) for r in con.execute("SELECT * FROM reviews WHERE document_id=?", (doc_id,))}

    def manual_lines(self, doc_id):
        with self.connection() as con:
            return [json.loads(r["data"]) for r in con.execute(
                "SELECT data FROM manual_lines WHERE document_id=? ORDER BY line_index", (doc_id,))]

    def add_manual_line(self, doc_id, line, review, actor):
        if not actor.strip():
            raise ValueError("Reviewer name is required")
        doc = self.document(doc_id)
        if doc["rfx_hash"] != self.rfx_hash() or not doc["extraction"]:
            raise ValueError("Manual lines require an extracted quote for the current RFx")
        extracted_count = len(json.loads(doc["extraction"])["lines"])
        with self.connection() as con:
            extra_count = con.execute(
                "SELECT COUNT(*) FROM manual_lines WHERE document_id=?", (doc_id,)).fetchone()[0]
            index = extracted_count + extra_count
            con.execute("INSERT INTO manual_lines(document_id,line_index,data) VALUES(?,?,?)",
                        (doc_id, index, dumps(line)))
            con.execute("INSERT INTO reviews(document_id,target,data) VALUES(?,?,?)",
                        (doc_id, str(index), dumps(review)))
            self._audit(con, "manual_line_added", f"{doc_id}:{index}", None,
                        {"raw": line, "review": review}, review["reason"], actor)
        return index

    def save_review(self, doc_id, target, value, actor):
        if not actor.strip():
            raise ValueError("Reviewer name is required")
        doc = self.document(doc_id)
        if doc["rfx_hash"] != self.rfx_hash():
            raise ValueError("This quote belongs to an earlier RFx; re-upload against the current RFx")
        with self.connection() as con:
            old = con.execute("SELECT data FROM reviews WHERE document_id=? AND target=?", (doc_id, target)).fetchone()
            before = json.loads(old[0]) if old else None
            if before is None and doc["extraction"]:
                raw = json.loads(doc["extraction"])
                if target.isdigit():
                    index = int(target)
                    all_lines = raw["lines"] + self.manual_lines(doc_id)
                    before = all_lines[index]
                else:
                    before = {"terms": raw["terms"], "quality": raw["quality"]}
            con.execute("INSERT INTO reviews(document_id,target,data) VALUES(?,?,?) ON CONFLICT(document_id,target) DO UPDATE SET data=excluded.data", (doc_id, target, dumps(value)))
            self._audit(con, "human_review", f"{doc_id}:{target}", before, value, value["reason"], actor)

    def audit(self):
        with self.connection() as con:
            return [dict(r) for r in con.execute("SELECT * FROM audit ORDER BY id DESC")]

    def reset_workspace(self):
        """Clear this configured workspace, including its audit, for an explicit demo reset."""
        with self.connection() as con:
            con.executescript("""
                DROP TRIGGER IF EXISTS audit_no_update;
                DROP TRIGGER IF EXISTS audit_no_delete;
                DELETE FROM reviews;
                DELETE FROM manual_lines;
                DELETE FROM documents;
                DELETE FROM state;
                DELETE FROM audit;
                DELETE FROM sqlite_sequence WHERE name='audit';
                CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
                    BEGIN SELECT RAISE(ABORT, 'Audit records are append-only'); END;
                CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
                    BEGIN SELECT RAISE(ABORT, 'Audit records are append-only'); END;
            """)
        for name in ("sources", "demo"):
            target = self.root / name
            if target.exists():
                shutil.rmtree(target)
        (self.root / "sources").mkdir(exist_ok=True)
