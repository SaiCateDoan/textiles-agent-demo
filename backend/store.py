"""SQLite is the metadata authority; LanceDB stores derived, revisioned vectors."""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from .lexical import tokens, fts_query
from .media import retrieval_text


def uid() -> str:
    return uuid.uuid4().hex[:16]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root / "uploads").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.path = root / "metadata.sqlite3"
        with self.connect() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS documents (
              id TEXT PRIMARY KEY, name TEXT, suffix TEXT, size INTEGER, status TEXT,
              progress INTEGER DEFAULT 0, error TEXT DEFAULT '', warnings TEXT DEFAULT '[]',
              created_at TEXT, updated_at TEXT, revision INTEGER DEFAULT 1,
              chunks INTEGER DEFAULT 0, pages INTEGER DEFAULT 0, ocr_pages INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS chunks (
              id TEXT PRIMARY KEY, document_id TEXT, ordinal INTEGER, text TEXT,
              locator TEXT, kind TEXT, fields TEXT, revision INTEGER DEFAULT 1,
              FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(document_id,ordinal);
            CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY,title TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS messages(
              id TEXT PRIMARY KEY,chat_id TEXT,role TEXT,payload TEXT,created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS images(
              id TEXT PRIMARY KEY, document_id TEXT NOT NULL, metadata TEXT NOT NULL,
              FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS images_doc ON images(document_id);
            CREATE TABLE IF NOT EXISTS datasets(
              id TEXT PRIMARY KEY, document_id TEXT NOT NULL, revision INTEGER NOT NULL,
              path TEXT NOT NULL, metadata TEXT NOT NULL,
              FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS datasets_doc ON datasets(document_id,revision);
            CREATE TABLE IF NOT EXISTS dataset_queries(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            """)
            for table, additions in {
                "documents": {"multimodal": "INTEGER DEFAULT 0", "image_count": "INTEGER DEFAULT 0", "retained_images": "INTEGER DEFAULT 0", "active_revision": "INTEGER DEFAULT 0", "content_hash": "TEXT DEFAULT ''", "columns_json": "TEXT DEFAULT '[]'"},
                "chunks": {"image_ids": "TEXT DEFAULT '[]'", "fts_indexed": "INTEGER DEFAULT 0"},
            }.items():
                existing = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                for column, declaration in additions.items():
                    if column not in existing:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
                        if column == "active_revision":
                            c.execute("UPDATE documents SET active_revision=revision WHERE status='ready'")
            c.executescript("""
            CREATE INDEX IF NOT EXISTS documents_status ON documents(status,created_at);
            CREATE INDEX IF NOT EXISTS documents_created ON documents(created_at DESC,id);
            CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id);
            CREATE INDEX IF NOT EXISTS documents_lane ON documents(multimodal,suffix,id);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(id UNINDEXED,document_id UNINDEXED,terms);
            CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON chunks BEGIN
                DELETE FROM chunk_fts WHERE rowid=old.rowid;
            END;
            """)
        (root / "assets").mkdir(exist_ok=True)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with c:
                yield c
        finally:
            c.close()

    def query(self, sql, args=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.connect() as c:
            c.execute(sql, args)

    def document(self, doc_id):
        rows = self.query("SELECT * FROM documents WHERE id=?", (doc_id,))
        return self.decode_doc(rows[0]) if rows else None

    def documents(self, limit=100, offset=0, q="", searchable=False, doc_ids=None):
        clauses, args = [], []
        if q:
            clauses.append("instr(lower(name),lower(?))>0"); args.append(q[:180])
        if searchable:
            clauses.append("(active_revision>0 OR status='ready')")
        if doc_ids is not None:
            if not doc_ids:
                return []
            clauses.append("id IN (" + ",".join("?" for _ in doc_ids) + ")"); args.extend(doc_ids)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return [self.decode_doc(r) for r in self.query("SELECT * FROM documents" + where + " ORDER BY created_at DESC,id LIMIT ? OFFSET ?", (*args, min(limit,1000), offset))]

    def document_count(self, q=""):
        return self.query("SELECT count(*) AS n FROM documents WHERE instr(lower(name),lower(?))>0", (q[:180],))[0]["n"]

    def statistics(self):
        return self.query("SELECT count(*) AS documents,coalesce(sum(status='ready'),0) AS ready,coalesce(sum(chunks),0) AS chunks,coalesce(sum(ocr_pages),0) AS ocr_pages,coalesce(sum(active_revision>0 OR status='ready'),0) AS searchable FROM documents")[0]

    @staticmethod
    def decode_doc(row):
        row["warnings"] = json.loads(row["warnings"])
        row["multimodal"] = bool(row["multimodal"])
        row["searchable"] = row["active_revision"] > 0 or row["status"] == "ready"
        row["columns"] = json.loads(row.pop("columns_json", "[]"))
        return row

    def update_doc(self, doc_id, **values):
        allowed = {"status", "progress", "error", "warnings", "revision", "chunks", "pages", "ocr_pages", "image_count", "retained_images", "active_revision", "content_hash", "columns_json"}
        if not set(values) <= allowed:
            raise ValueError("Invalid metadata field")
        values["updated_at"] = now()
        self.execute("UPDATE documents SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?", (*values.values(), doc_id))

    def chunks(self, doc_id=None, limit=None, offset=0):
        sql = "SELECT c.*,d.name AS document_name,d.status FROM chunks c JOIN documents d ON d.id=c.document_id"
        args = ()
        if doc_id:
            sql += " WHERE document_id=?"
            args = (doc_id,)
        sql += " ORDER BY c.document_id,c.ordinal"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args = (*args, limit, offset)
        rows = self.query(sql, args)
        return self.attach_images(rows)

    def attach_images(self, rows):
        ids = list({i for r in rows for i in json.loads(r["image_ids"] or "[]")})
        image_rows = []
        for start in range(0,len(ids),500):
            batch = ids[start:start+500]
            image_rows.extend(self.query("SELECT id,metadata FROM images WHERE id IN (" + ",".join("?" for _ in batch) + ")", batch))
        images = {im["id"]: json.loads(im["metadata"]) for im in image_rows}
        for r in rows:
            r["fields"] = json.loads(r["fields"] or "{}")
            r["image_ids"] = json.loads(r["image_ids"] or "[]")
            r["images"] = [images[i] for i in r["image_ids"] if i in images]
        return rows

    def replace_chunks(self, doc_id, chunks, images=None):
        prepared = self.prepare_chunks(chunks)
        with self.connect() as c:
            self.write_chunks(c, doc_id, chunks, prepared, images)
            if chunks:
                c.execute("UPDATE documents SET active_revision=?,revision=? WHERE id=?",(chunks[0].get("revision",1),chunks[0].get("revision",1),doc_id))

    @staticmethod
    def prepare_chunks(chunks):
        return [" ".join(tokens(retrieval_text(x))) for x in chunks]

    @staticmethod
    def write_chunks(c, doc_id, chunks, prepared, images=None):
        for im in images or []:
            c.execute("INSERT OR IGNORE INTO images(id,document_id,metadata) VALUES(?,?,?)", (im["id"], doc_id, json.dumps(im, ensure_ascii=False)))
        c.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
        for i, (x, terms) in enumerate(zip(chunks, prepared)):
            cur = c.execute("INSERT INTO chunks(id,document_id,ordinal,text,locator,kind,fields,revision,image_ids,fts_indexed) VALUES(?,?,?,?,?,?,?,?,?,1)",
                (x["id"],doc_id,i,x["text"],x["locator"],x["kind"],json.dumps(x.get("fields",{}),ensure_ascii=False),x.get("revision",1),json.dumps(x.get("image_ids",[]))))
            c.execute("INSERT INTO chunk_fts(rowid,id,document_id,terms) VALUES(?,?,?,?)", (cur.lastrowid,x["id"],doc_id,terms))
        columns = sorted({k for x in chunks for k in (
            [c["name"] for c in x.get("fields",{}).get("columns",[])] if x["kind"]=="dataset" else
            x.get("fields",{}).get("headers",[]) if x["kind"]=="document_table" else
            x.get("fields",{}) if x["kind"]=="table" else [])})
        c.execute("UPDATE documents SET columns_json=? WHERE id=?", (json.dumps(columns,ensure_ascii=False),doc_id))

    def chunks_by_ids(self, ids):
        if not ids:
            return []
        rows = self.query("SELECT c.*,d.name AS document_name,d.status FROM chunks c JOIN documents d ON d.id=c.document_id WHERE c.id IN (" + ",".join("?" for _ in ids) + ") AND (d.active_revision=c.revision OR (d.active_revision=0 AND d.status='ready' AND d.revision=c.revision))", ids)
        return self.attach_images(rows)

    def lexical(self, text, doc_ids=None, limit=30):
        query = fts_query(text)
        if not query:
            return []
        args, clause = [query], ""
        if doc_ids is not None:
            if not doc_ids:
                return []
            clause = " AND c.document_id IN (" + ",".join("?" for _ in doc_ids) + ")"
            args.extend(doc_ids)
        return self.query("SELECT c.id,c.revision,bm25(chunk_fts) AS rank FROM chunk_fts JOIN chunks c ON c.rowid=chunk_fts.rowid JOIN documents d ON d.id=c.document_id WHERE chunk_fts MATCH ? AND (d.active_revision=c.revision OR (d.active_revision=0 AND d.status='ready' AND d.revision=c.revision))" + clause + " ORDER BY rank LIMIT ?", (*args,limit))

    def backfill_fts(self, limit=256):
        rows = self.query("SELECT c.*,d.name AS document_name,d.status FROM chunks c JOIN documents d ON d.id=c.document_id WHERE fts_indexed=0 LIMIT ?", (limit,))
        self.attach_images(rows)
        prepared = self.prepare_chunks(rows)
        with self.connect() as c:
            for row, terms in zip(rows,prepared):
                found = c.execute("SELECT rowid FROM chunks WHERE id=? AND revision=? AND fts_indexed=0", (row["id"],row["revision"])).fetchone()
                if found:
                    c.execute("INSERT OR REPLACE INTO chunk_fts(rowid,id,document_id,terms) VALUES(?,?,?,?)", (found[0],row["id"],row["document_id"],terms))
                    c.execute("UPDATE chunks SET fts_indexed=1 WHERE id=?", (row["id"],))
            for doc_id in {r["document_id"] for r in rows}:
                columns = [r[0] for r in c.execute("SELECT DISTINCT j.key FROM chunks c,json_each(c.fields) j WHERE c.document_id=? ORDER BY j.key",(doc_id,))]
                c.execute("UPDATE documents SET columns_json=? WHERE id=? AND columns_json='[]'",(json.dumps(columns,ensure_ascii=False),doc_id))
        return len(rows)

    def image(self, image_id):
        rows = self.query("SELECT document_id,metadata FROM images WHERE id=?", (image_id,))
        return {"document_id": rows[0]["document_id"], **json.loads(rows[0]["metadata"])} if rows else None

    def add_message(self, chat_id, role, payload):
        message_id=uid()
        self.execute("INSERT INTO messages VALUES(?,?,?,?,?)", (message_id, chat_id, role, json.dumps(payload, ensure_ascii=False), now()))
        return message_id

    def begin_turn(self, chat_id, content, message_id, initial):
        with self.connect() as c:
            c.execute("INSERT INTO messages VALUES(?,?,?,?,?)",(uid(),chat_id,"user",json.dumps({"content":content},ensure_ascii=False),now()))
            c.execute("INSERT INTO messages VALUES(?,?,?,?,?)",(message_id,chat_id,"assistant",json.dumps(initial,ensure_ascii=False),now()))
            c.execute("UPDATE chats SET title=? WHERE id=?",(content[:30],chat_id))

    def update_message(self, message_id, payload):
        self.execute("UPDATE messages SET payload=? WHERE id=? AND role='assistant'",(json.dumps(payload,ensure_ascii=False),message_id))

    def messages(self, chat_id, limit=None):
        rows = self.query("SELECT * FROM messages WHERE chat_id=? ORDER BY rowid" + (" DESC LIMIT ?" if limit else ""),(chat_id,limit) if limit else (chat_id,))
        if limit:
            rows.reverse()
        return [{**json.loads(x["payload"]),"id":x["id"],"role":x["role"],"created_at":x["created_at"]}
                for x in rows]
