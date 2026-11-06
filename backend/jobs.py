"""SQLite-backed at-least-once jobs with leases and fenced publication.

All workers must share the same local filesystem. This is not a distributed broker.
"""
import json
import time
import sqlite3

from .store import now, uid


class QueueFull(ValueError):
    pass


class Conflict(ValueError):
    pass


class LeaseLost(RuntimeError):
    pass


class RetryableError(RuntimeError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class JobQueue:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        with store.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS ingest_jobs(
                id TEXT PRIMARY KEY,document_id TEXT NOT NULL,revision INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued',stage TEXT DEFAULT 'queued',attempts INTEGER DEFAULT 0,
                available_at REAL DEFAULT 0,lease_until REAL DEFAULT 0,owner TEXT DEFAULT '',
                error TEXT DEFAULT '',created_at TEXT,updated_at TEXT,
                UNIQUE(document_id,revision),
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS jobs_ready ON ingest_jobs(state,available_at,lease_until);
            CREATE TABLE IF NOT EXISTS resource_slots(name TEXT,slot INTEGER,owner TEXT,lease_until REAL,PRIMARY KEY(name,slot));
            CREATE TABLE IF NOT EXISTS rate_events(name TEXT,at REAL);
            CREATE INDEX IF NOT EXISTS rate_events_age ON rate_events(name,at);
            CREATE TABLE IF NOT EXISTS upload_keys(key TEXT PRIMARY KEY,document_id TEXT,
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS mutations(document_id TEXT PRIMARY KEY,owner TEXT,lease_until REAL,previous_status TEXT,
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS garbage(document_id TEXT PRIMARY KEY,suffix TEXT,created_at TEXT);
            """)
            # Migration of pre-queue imports. No coroutine per queued document.
            c.execute("INSERT OR IGNORE INTO ingest_jobs(id,document_id,revision,created_at,updated_at) SELECT lower(hex(randomblob(8))),id,revision,?,? FROM documents WHERE status IN ('queued','parsing','analyzing','indexing')", (now(),now()))
            if "checkpoint_cleaned" not in {r["name"] for r in c.execute("PRAGMA table_info(ingest_jobs)")}:
                c.execute("ALTER TABLE ingest_jobs ADD COLUMN checkpoint_cleaned INTEGER DEFAULT 0")
            c.execute("UPDATE documents SET status='error',error='上次修改被中断，请核对原文后重新处理',updated_at=? WHERE status IN ('editing','deleting') AND id NOT IN (SELECT document_id FROM mutations)",(now(),))

    def _capacity(self, c):
        count = c.execute("SELECT count(*) FROM ingest_jobs WHERE state IN ('queued','running')").fetchone()[0]
        if count >= self.settings.ingestion_queue_limit:
            raise QueueFull("入库队列已满，请稍后重试；现有任务继续处理")

    def upload(self, doc, key=None):
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if key:
                existing = c.execute("SELECT document_id FROM upload_keys WHERE key=?", (key,)).fetchone()
                if existing:
                    old = c.execute("SELECT content_hash,multimodal FROM documents WHERE id=?", (existing[0],)).fetchone()
                    if old[0] != doc["content_hash"] or bool(old[1]) != bool(doc["multimodal"]):
                        raise Conflict("同一上传重试标识不能用于不同文件或入库模式")
                    return existing[0]
            self._capacity(c)
            c.execute("INSERT INTO documents(id,name,suffix,size,status,created_at,updated_at,multimodal,content_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                (doc["id"],doc["name"],doc["suffix"],doc["size"],"queued",now(),now(),doc["multimodal"],doc["content_hash"]))
            if key:
                c.execute("INSERT INTO upload_keys VALUES(?,?)", (key,doc["id"]))
            self._insert(c,doc["id"],1)
            return doc["id"]

    @staticmethod
    def _insert(c, doc_id, revision):
        c.execute("INSERT INTO ingest_jobs(id,document_id,revision,created_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(document_id,revision) DO UPDATE SET state='queued',attempts=0,owner='',lease_until=0,available_at=0,error='',checkpoint_cleaned=0,updated_at=excluded.updated_at",
            (uid(),doc_id,revision,now(),now()))

    def enqueue(self, doc_id, expected_revision=None, reprocess=False, confirm=False):
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            doc = c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
            if not doc:
                raise Conflict("文档不存在")
            if doc["status"] not in ({"ready","error"} if reprocess else {"error"}):
                raise Conflict("文档正在处理中，或不需要重试")
            if expected_revision is not None and expected_revision != doc["revision"]:
                raise Conflict("文档版本已变化，请刷新后重新解析")
            if reprocess and doc["revision"] > 1 and not confirm:
                raise Conflict("重新解析将覆盖已有校对，请明确确认后继续")
            self._capacity(c)
            revision = doc["revision"] + int(reprocess)
            c.execute("UPDATE documents SET status='queued',revision=?,progress=0,error='',updated_at=? WHERE id=?", (revision,now(),doc_id))
            self._insert(c,doc_id,revision)
            return revision

    def claim(self, fast_only=False):
        stamp = time.time()
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            exhausted = c.execute("SELECT id,document_id FROM ingest_jobs WHERE state='running' AND lease_until<? AND attempts>=? LIMIT 100", (stamp,self.settings.ingestion_max_attempts)).fetchall()
            for row in exhausted:
                c.execute("UPDATE ingest_jobs SET state='failed',owner='',error='任务反复中断，已达到重试上限',updated_at=? WHERE id=?", (now(),row["id"]))
                c.execute("UPDATE documents SET status='error',error='任务反复中断，已达到重试上限',updated_at=? WHERE id=?", (now(),row["document_id"]))
            lane = " AND document_id IN (SELECT id FROM documents WHERE multimodal=0 AND suffix IN ('.txt','.md','.csv','.tsv','.xlsx','.xls','.html','.htm'))" if fast_only else ""
            row = c.execute("SELECT * FROM ingest_jobs WHERE (state='queued' AND available_at<=? OR state='running' AND lease_until<?) AND attempts<?"+lane+" ORDER BY available_at,created_at,id LIMIT 1", (stamp,stamp,self.settings.ingestion_max_attempts)).fetchone()
            if not row:
                return None
            owner = uid()
            c.execute("UPDATE ingest_jobs SET state='running',attempts=attempts+1,owner=?,lease_until=?,updated_at=? WHERE id=?", (owner,stamp+self.settings.ingestion_lease_seconds,now(),row["id"]))
            return dict(c.execute("SELECT * FROM ingest_jobs WHERE id=?", (row["id"],)).fetchone())

    def heartbeat(self, job):
        stamp = time.time()
        with self.store.connect() as c:
            changed = c.execute("UPDATE ingest_jobs SET lease_until=?,updated_at=? WHERE id=? AND state='running' AND owner=? AND lease_until>?", (stamp+self.settings.ingestion_lease_seconds,now(),job["id"],job["owner"],stamp)).rowcount
            if changed:
                c.execute("UPDATE resource_slots SET lease_until=? WHERE owner=?", (stamp+self.settings.ingestion_lease_seconds,job["owner"]))
            return bool(changed)

    @staticmethod
    def check(c, job):
        row = c.execute("SELECT 1 FROM ingest_jobs WHERE id=? AND owner=? AND state='running' AND lease_until>?", (job["id"],job["owner"],time.time())).fetchone()
        if not row:
            raise LeaseLost("任务已由其他工作进程接管")

    def stage(self, job, stage, progress, **values):
        allowed = {"image_count","retained_images"}
        if set(values)-allowed:
            raise ValueError("Invalid stage fields")
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self.check(c,job)
            c.execute("UPDATE ingest_jobs SET stage=?,updated_at=? WHERE id=?", (stage,now(),job["id"]))
            assignments = ",".join(f"{k}=?" for k in values)
            c.execute("UPDATE documents SET status=?,progress=?,error='',updated_at=?" + (","+assignments if assignments else "") + " WHERE id=? AND revision=?", (stage,progress,now(),*values.values(),job["document_id"],job["revision"]))

    def publish(self, job, chunks, prepared, assets, result, datasets=None):
        # Everything expensive is prepared outside this short metadata transaction.
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self.check(c,job)
            row = c.execute("SELECT revision FROM documents WHERE id=?", (job["document_id"],)).fetchone()
            if not row or row[0] != job["revision"]:
                raise LeaseLost("目标文档版本已变化")
            self.store.write_chunks(c,job["document_id"],chunks,prepared,assets)
            from .datasets import write_datasets
            write_datasets(c,job["document_id"],job["revision"],datasets or [])
            c.execute("UPDATE documents SET active_revision=?,status='ready',progress=100,chunks=?,pages=?,ocr_pages=?,warnings=?,image_count=?,retained_images=?,error='',updated_at=? WHERE id=?",
                (job["revision"],len(chunks),result.pages,result.ocr_pages,json.dumps(result.warnings,ensure_ascii=False),len(assets),sum(im["decision"]=="retain" for im in assets),now(),job["document_id"]))
            c.execute("UPDATE ingest_jobs SET state='done',stage='ready',owner='',lease_until=0,error='',updated_at=? WHERE id=?", (now(),job["id"]))
            c.execute("DELETE FROM resource_slots WHERE owner=?", (job["owner"],))

    def fail(self, job, message, retry=False, delay=None):
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                self.check(c,job)
            except LeaseLost:
                return
            again = retry and job["attempts"] < self.settings.ingestion_max_attempts
            c.execute("UPDATE ingest_jobs SET state=?,available_at=?,owner='',lease_until=0,error=?,updated_at=? WHERE id=?", ("queued" if again else "failed",time.time()+(delay if delay is not None else min(60,2**job["attempts"])),message[:600],now(),job["id"]))
            c.execute("UPDATE documents SET status=?,error=?,updated_at=? WHERE id=?", ("queued" if again else "error",message[:600],now(),job["document_id"]))
            c.execute("DELETE FROM resource_slots WHERE owner=?", (job["owner"],))

    def release(self, job):
        # Graceful shutdown is not a failed attempt; successful checkpoints survive.
        with self.store.connect() as c:
            c.execute("UPDATE ingest_jobs SET state='queued',owner='',lease_until=0,available_at=0,attempts=max(0,attempts-1),updated_at=? WHERE id=? AND owner=? AND state='running'", (now(),job["id"],job["owner"]))
            c.execute("DELETE FROM resource_slots WHERE owner=?", (job["owner"],))

    def resource(self, job, name, capacity, rpm=None):
        stamp = time.time()
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self.check(c,job)
            c.execute("DELETE FROM resource_slots WHERE lease_until<?", (stamp,))
            if rpm:
                c.execute("DELETE FROM rate_events WHERE at<?", (stamp-60,))
                if c.execute("SELECT count(*) FROM rate_events WHERE name=?", (name,)).fetchone()[0] >= rpm:
                    return None
            occupied = {r[0] for r in c.execute("SELECT slot FROM resource_slots WHERE name=?", (name,))}
            slot = next((i for i in range(capacity) if i not in occupied),None)
            if slot is None:
                return None
            c.execute("INSERT INTO resource_slots VALUES(?,?,?,?)", (name,slot,job["owner"],stamp+self.settings.ingestion_lease_seconds))
            if rpm:
                c.execute("INSERT INTO rate_events VALUES(?,?)", (name,stamp))
            return slot

    def free_resource(self, job, name, slot):
        with self.store.connect() as c:
            c.execute("DELETE FROM resource_slots WHERE name=? AND slot=? AND owner=?", (name,slot,job["owner"]))

    def statistics(self):
        counts = self.store.query("SELECT state,count(*) AS n FROM ingest_jobs GROUP BY state")
        return {**{name:0 for name in ("queued","running","done","failed")},**{r["state"]:r["n"] for r in counts}}

    def reserve_mutation(self, doc_id, status, revision=None):
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self._recover_mutations(c)
            row = c.execute("SELECT status,revision FROM documents WHERE id=?",(doc_id,)).fetchone()
            if not row or row["status"] not in ({"ready"} if status=="editing" else {"ready","error"}):
                raise Conflict("文档正在处理中，请稍后再试")
            if revision is not None and revision != row["revision"]:
                raise Conflict("分块已被修改，请刷新后编辑")
            owner = uid()
            try:
                c.execute("INSERT INTO mutations VALUES(?,?,?,?)",(doc_id,owner,time.time()+self.settings.ingestion_lease_seconds,row["status"]))
            except sqlite3.IntegrityError:
                raise Conflict("文档正在更新，请稍后再试") from None
            c.execute("UPDATE documents SET status=?,updated_at=? WHERE id=?",(status,now(),doc_id))
            return {"document_id":doc_id,"owner":owner,"previous_status":row["status"]}

    def renew_mutation(self, mutation):
        with self.store.connect() as c:
            return bool(c.execute("UPDATE mutations SET lease_until=? WHERE document_id=? AND owner=? AND lease_until>?",(time.time()+self.settings.ingestion_lease_seconds,mutation["document_id"],mutation["owner"],time.time())).rowcount)

    @staticmethod
    def check_mutation(c, mutation):
        if not c.execute("SELECT 1 FROM mutations WHERE document_id=? AND owner=? AND lease_until>?",(mutation["document_id"],mutation["owner"],time.time())).fetchone():
            raise Conflict("文档更新租约已失效，请刷新后重试")

    def release_mutation(self, mutation):
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("DELETE FROM mutations WHERE document_id=? AND owner=?",(mutation["document_id"],mutation["owner"])).rowcount:
                c.execute("UPDATE documents SET status=?,updated_at=? WHERE id=? AND status IN ('editing','deleting')",(mutation["previous_status"],now(),mutation["document_id"]))

    @staticmethod
    def _recover_mutations(c):
        expired = c.execute("SELECT document_id,previous_status FROM mutations WHERE lease_until<?",(time.time(),)).fetchall()
        for row in expired:
            c.execute("UPDATE documents SET status=?,updated_at=? WHERE id=? AND status IN ('editing','deleting')",(row["previous_status"],now(),row["document_id"]))
            c.execute("DELETE FROM mutations WHERE document_id=?",(row["document_id"],))

    def recover_mutations(self):
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self._recover_mutations(c)

    def publish_edit(self, mutation, chunks, prepared, revision):
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self.check_mutation(c,mutation)
            self.store.write_chunks(c,mutation["document_id"],chunks,prepared)
            c.execute("UPDATE documents SET status='ready',revision=?,active_revision=?,updated_at=? WHERE id=?",(revision,revision,now(),mutation["document_id"]))

    def delete_document(self, mutation):
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self.check_mutation(c,mutation)
            c.execute("DELETE FROM dataset_queries WHERE EXISTS (SELECT 1 FROM json_each(dataset_queries.payload,'$.datasets') d WHERE json_extract(d.value,'$.document_id')=?)",(mutation["document_id"],))
            c.execute("INSERT OR REPLACE INTO garbage SELECT id,suffix,? FROM documents WHERE id=?",(now(),mutation["document_id"]))
            c.execute("DELETE FROM documents WHERE id=?",(mutation["document_id"],))
