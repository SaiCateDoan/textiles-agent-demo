import math
import re
import threading
import time
from importlib.metadata import version
from array import array
from contextlib import contextmanager
from collections import defaultdict

import jieba
import lancedb
from lancedb.index import BTree, IvfFlat
import pyarrow as pa
from filelock import FileLock

from .media import retrieval_text
from .cache import ContentCache, digest
from .config import settings
from .charts import chart


class EmbeddingGate:
    """Queries have priority between bounded ingestion batches, not mid-inference."""
    def __init__(self):
        self.condition = threading.Condition()
        self.busy = False
        self.queries = 0

    @contextmanager
    def enter(self, query=False):
        with self.condition:
            self.queries += int(query)
            try:
                while self.busy or (not query and self.queries):
                    self.condition.wait()
                self.busy = True
            finally:
                self.queries -= int(query)
        try:
            yield
        finally:
            with self.condition:
                self.busy = False
                self.condition.notify_all()


class VectorIndex:
    def __init__(self, root, model, provider="CPUExecutionProvider"):
        self.root, self.model_name, self.provider = root, model, provider
        self.db = lancedb.connect(str(root / "lancedb"))
        self.lock = threading.RLock()
        self.embedding_gate = EmbeddingGate()
        (root/"locks").mkdir(exist_ok=True)
        self.cache = ContentCache(root,settings.cache_max_mb)
        self.embedding_fingerprint = digest("bge-v1",model,provider,version("fastembed"),settings.embedding_cache_version)
        self.last_maintenance = 0
        self._model = None
        self.schema = pa.schema([("id", pa.string()), ("document_id", pa.string()), ("revision", pa.int64()), ("vector", pa.list_(pa.float32(), 512))])
        self.table = self.db.create_table("chunks", schema=self.schema, exist_ok=True)

    def embed(self, texts, query=False):
        from fastembed import TextEmbedding
        result = []
        for start in range(0,len(texts),settings.embedding_batch_size):
            batch = texts[start:start+settings.embedding_batch_size]
            with self.embedding_gate.enter(query):
                if self._model is None:
                    self._model = TextEmbedding(model_name=self.model_name, cache_dir=str(settings.embedding_cache_dir or self.root / "models"), threads=4, providers=[self.provider])
                vectors = self._model.query_embed(batch) if query else self._model.embed(batch,batch_size=settings.embedding_batch_size)
                result.extend(v.tolist() for v in vectors)
        return result

    def cached_vectors(self, texts):
        keys = [digest(self.embedding_fingerprint,t) for t in texts]
        result = [None]*len(keys)
        missing = {}
        cached_values = self.cache.get_many("embedding",keys)
        for i,key in enumerate(keys):
            cached = cached_values.get(key)
            if cached is not None:
                vector = array("f")
                vector.frombytes(cached)
                if len(vector) == 512:
                    result[i] = list(vector)
                    continue
            missing.setdefault(key,[]).append(i)
        if missing:
            unique = list(missing)
            vectors = self.embed([texts[missing[k][0]] for k in unique])
            if len(vectors) != len(unique):
                raise ValueError("嵌入返回数量不匹配")
            pairs = []
            for key,vector in zip(unique,vectors):
                if len(vector) != 512 or not all(math.isfinite(v) for v in vector):
                    raise ValueError("嵌入维度或数值无效")
                pairs.append((key,array("f",vector).tobytes()))
                for i in missing[key]:
                    result[i] = vector
            self.cache.put_many("embedding",pairs)
        return result

    @contextmanager
    def writer(self):
        with self.lock, FileLock(str(self.root/"locks"/"vectors.lock"),timeout=30):
            self.table.checkout_latest()
            yield

    def stage(self, doc_id, chunks, revision):
        # Each retry replaces only its unpublished target revision. Active vectors
        # remain visible until metadata publication. Never hold the write lock
        # while embedding a document.
        with self.writer():
            self.table.delete(f"document_id = '{doc_id}' AND revision = {int(revision)}")
        size = settings.vector_write_batch_size
        for start in range(0,len(chunks),size):
            batch = chunks[start:start+size]
            vectors = self.cached_vectors([retrieval_text(c) for c in batch])
            with self.writer():
                self.table.add([dict(id=c["id"],document_id=doc_id,revision=revision,vector=v) for c,v in zip(batch,vectors)])
        with self.writer():
            actual = self.table.count_rows(f"document_id = '{doc_id}' AND revision = {int(revision)}")
            if actual != len(chunks):
                raise ValueError("向量数量与分块数量不一致，已停止发布")

    def cleanup(self, doc_id, revision):
        with self.writer():
            self.table.delete(f"document_id = '{doc_id}' AND revision < {int(revision)}")

    def discard(self, doc_id, revision):
        with self.writer():
            self.table.delete(f"document_id = '{doc_id}' AND revision = {int(revision)}")

    def replace(self, doc_id, chunks, revision):
        self.stage(doc_id,chunks,revision)
        self.cleanup(doc_id,revision)

    def delete(self, doc_id):
        with self.writer():
            self.table.delete(f"document_id = '{doc_id}'")

    def maintain(self):
        # One maintenance process at a time; do not run compaction per document.
        with FileLock(str(self.root/"locks"/"maintenance.lock"),timeout=0):
            if time.time()-self.last_maintenance < 300:
                return
            self.last_maintenance = time.time()
            with FileLock(str(self.root/"locks"/"vectors.lock"),timeout=0):
                # Dedicated handle: index training/compaction cannot hold the
                # process-local lock needed by online readers.
                table = self.db.open_table("chunks")
                count = table.count_rows()
                if count < 10000:
                    return
                indices = list(table.list_indices())
                columns = {col for idx in indices for col in idx.columns}
                if "document_id" not in columns:
                    table.create_index("document_id",config=BTree())
                if "vector" not in columns:
                    table.create_index("vector",config=IvfFlat(distance_type="cosine",num_partitions=max(1,min(256,int(math.sqrt(count))))))
                table.optimize()

    def search(self, store, query, doc_ids=None, limit=8):
        if doc_ids is not None and not doc_ids:
            return []
        if doc_ids and any(not re.fullmatch(r"[a-f0-9]{16}",i) for i in doc_ids):
            raise ValueError("无效文档ID")
        lexical = store.lexical(query,doc_ids)
        vector = self.embed([query], query=True)[0]
        # Predicates are constructed only from validated server IDs. Oversample
        # bounded candidates, then verify active metadata and revision.
        with self.lock:
            self.table.checkout_latest()
            search = self.table.search(vector).distance_type("cosine")
            if doc_ids:
                search = search.where("document_id IN ("+",".join("'"+i+"'" for i in doc_ids)+")",prefilter=True)
            ranked = search.limit(max(64,limit*8)).to_list()
        rows = store.chunks_by_ids(list(dict.fromkeys([r["id"] for r in ranked]+[r["id"] for r in lexical])))
        valid = {x["id"]:x for x in rows}
        fused = defaultdict(float)
        distances = {}
        for rank, row in enumerate(ranked):
            cid = row["id"]
            if cid in valid and row["revision"] == valid[cid]["revision"]:
                fused[cid] += 1 / (60+rank+1)
                distances[cid] = row["_distance"]
        for rank, row in enumerate(lexical):
            if row["id"] in valid and row["revision"] == valid[row["id"]]["revision"]:
                fused[row["id"]] += 1 / (60+rank+1)
        results = []
        for cid in sorted(fused, key=fused.get, reverse=True):
            chunk = dict(valid[cid])
            # BGE distance is not a calibrated confidence. Report it only as ranking metadata.
            chunk.update(score=round(fused[cid], 5), distance=distances.get(cid))
            results.append(chunk)
            if len(results) == limit:
                break
        return results


def number(value):
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    value = str(value).strip().replace(",", "")
    # Preserve units in column names. Reject dates, ranges, inequalities and mixed text.
    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?%?", value):
        return None
    out = float(value.rstrip("%"))
    return out if math.isfinite(out) else None


def table_rows(store, doc_id):
    doc = store.document(doc_id)
    if not doc or not doc["searchable"]:
        raise ValueError("文档不存在或索引未就绪")
    rows = [x for x in store.chunks(doc_id) if x["kind"] == "table" and x["fields"]]
    if not rows:
        raise ValueError("该文档没有结构化表格，请导入 CSV/Excel 或可识别的带表格文档")
    return rows


def scenario(store, document_id, parameter, metric, direction="min"):
    if direction not in {"min", "max"}:
        raise ValueError("direction 必须为 min 或 max")
    rows = table_rows(store, document_id)
    pairs = [(number(r["fields"].get(parameter)), number(r["fields"].get(metric)), r) for r in rows]
    pairs = [(x, y, r) for x, y, r in pairs if x is not None and y is not None]
    if len(pairs) < 3 or len({x for x, _, _ in pairs}) < 2:
        raise ValueError("交互控件至少需要 3 条有效工况、2 个不同参数值；列名需包含完整单位")
    lo, hi = min(x for x, _, _ in pairs), max(x for x, _, _ in pairs)
    best = sorted(pairs, key=lambda p: p[1], reverse=direction == "max")[0]
    return dict(type="scenario", title=f"{parameter} 工况探索", document_id=document_id, parameter=parameter, metric=metric, direction=direction,
                min=lo, max=hi, step=round((hi-lo)/100, 4), default=best[0],
                observed=[dict(x=x, y=y, source_id=r["id"]) for x, y, r in pairs], source_ids=[r["id"] for _, _, r in pairs],
                note="基于已导入历史工况的近邻参考，非因果预测；仅在观测范围内探索，生产调整需工艺验证。",revision=pairs[0][2]["revision"],_source_rows=[r for _,_,r in pairs])


def evaluate(store, document_id, parameter, metric, value, direction="min"):
    spec = scenario(store, document_id, parameter, metric, direction)
    if not math.isfinite(value) or not spec["min"] <= value <= spec["max"]:
        raise ValueError("参数超出历史观测范围")
    points = sorted(spec["observed"], key=lambda p: abs(p["x"]-value))
    exact = [p for p in points if abs(p["x"]-value) < 1e-6]
    nearby = exact or points[:3]
    avg = sum(p["y"] for p in nearby)/len(nearby)
    best = sorted(points, key=lambda p: p["y"], reverse=direction == "max")[0]
    return dict(value=value, reference=round(avg, 4), metric=metric, parameter=parameter, nearest=nearby,
                recommendation=f"当前 {parameter}={value:g}，{len(nearby)} 条邻近工况的 {metric} 均值为 {avg:.3f}。历史单条{'最小' if direction == 'min' else '最大'}值为 {best['y']:g}，对应 {parameter}={best['x']:g}。这不是最优生产配方，请结合质量、能耗及小样验证。",
                source_ids=[p["source_id"] for p in nearby], note=spec["note"])
