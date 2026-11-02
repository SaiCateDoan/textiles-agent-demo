import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.checkpoints import load_extraction, save_extraction
from backend.config import Settings
from backend.ingest import extract
from backend.jobs import Conflict, JobQueue, LeaseLost, QueueFull
from backend.pipeline import IngestionRunner, run_parser
from backend.store import Store, uid
from conftest import load_doc
from test_multimodal import FakeVision, png


def configuration(root, **values):
    return Settings(_env_file=None,textile_data_dir=root,deepseek_api_key="test-only",
                    ingestion_poll_seconds=.02,ingestion_lease_seconds=3,**values)


def enqueue(queue, name="test.txt", data="纺织工艺正常内容".encode(), multimodal=False, key=None):
    doc_id = uid()
    suffix = Path(name).suffix
    (queue.store.root/"uploads"/(doc_id+suffix)).write_bytes(data)
    queue.upload(dict(id=doc_id,name=name,suffix=suffix,size=len(data),multimodal=multimodal,content_hash=""),key)
    return doc_id


async def local_parser(folder, timeout):
    from backend.multimodal import collect
    req = json.loads((folder/"request.json").read_text(encoding="utf8"))
    result = await asyncio.to_thread(collect if req["multimodal"] else extract,Path(req["path"]),req["suffix"])
    await asyncio.to_thread(save_extraction,folder,result,req["fingerprint"])


async def wait_until(predicate, timeout=10):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():
            return
        await asyncio.sleep(.02)
    raise AssertionError("Condition was not reached before deadline")


def test_queue_capacity_is_atomic_across_producers(storage):
    store,_ = storage
    queue = JobQueue(store,configuration(store.root,ingestion_queue_limit=5))
    def submit(_):
        try:
            return enqueue(queue)
        except QueueFull:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(submit,range(30)))
    assert sum(i is not None for i in ids)==5
    assert queue.statistics()["queued"]==5 and store.document_count()==5


def test_claim_is_exclusive_and_expired_owner_cannot_publish(storage):
    store,_ = storage
    queue = JobQueue(store,configuration(store.root))
    doc_id = enqueue(queue)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = [j for j in pool.map(lambda _:queue.claim(),range(16)) if j]
    assert len(claims)==1
    old = claims[0]
    store.execute("UPDATE ingest_jobs SET lease_until=0 WHERE id=?",(old["id"],))
    replacement = queue.claim()
    assert replacement["owner"] != old["owner"] and replacement["attempts"]==2
    with pytest.raises(LeaseLost):
        queue.stage(old,"indexing",55)
    with pytest.raises(LeaseLost):
        queue.publish(old,[],[],[],extract(store.root/"uploads"/(doc_id+".txt"),".txt"))
    queue.fail(old,"stale worker",retry=False)
    assert store.document(doc_id)["status"]=="queued"
    assert queue.heartbeat(replacement)


def test_global_resource_and_request_budget(storage):
    store,_ = storage
    queue = JobQueue(store,configuration(store.root))
    for _ in range(3):
        enqueue(queue)
    jobs = [queue.claim() for _ in range(3)]
    assert queue.resource(jobs[0],"vision",1,rpm=2)==0
    assert queue.resource(jobs[1],"vision",1,rpm=2) is None
    queue.free_resource(jobs[0],"vision",0)
    assert queue.resource(jobs[1],"vision",1,rpm=2)==0
    queue.free_resource(jobs[1],"vision",0)
    assert queue.resource(jobs[2],"vision",1,rpm=2) is None
    store.execute("UPDATE rate_events SET at=0")
    assert queue.resource(jobs[2],"vision",1,rpm=2)==0


async def test_slow_document_does_not_block_other_workers_and_restart(storage):
    store,index = storage
    slow = asyncio.Event()
    async def parser(folder, timeout):
        req = json.loads((folder/"request.json").read_text(encoding="utf8"))
        if Path(req["path"]).read_text(encoding="utf8")=="slow":
            await slow.wait()
        await local_parser(folder,timeout)
    config = configuration(store.root)
    runner = IngestionRunner(store,index,config,FakeVision,parser)
    first = enqueue(runner.queue,data=b"slow")
    second = enqueue(runner.queue,data="快速完成的纺织资料".encode())
    runner.start()
    try:
        await wait_until(lambda:store.document(second)["status"]=="ready")
        assert store.document(first)["status"]=="parsing"
        assert len(runner.workers)==config.ingestion_workers+1
    finally:
        await runner.stop()
    assert runner.queue.statistics()["queued"]==1
    restarted = IngestionRunner(store,index,config,FakeVision,local_parser)
    restarted.start()
    try:
        await wait_until(lambda:store.document(first)["status"]=="ready")
    finally:
        await restarted.stop()


async def test_parser_timeout_kills_disposable_process(tmp_path):
    # Real process spawn/kill, with a deadline shorter than interpreter startup.
    with pytest.raises(TimeoutError):
        await run_parser(tmp_path,.001)


async def test_partial_vision_success_survives_retry_without_reparsing(storage):
    import base64
    store,index = storage
    calls = []
    parses = []
    failing = True
    class Vision(FakeVision):
        async def analyze(self,image,context):
            calls.append(image.id)
            if image.locator.endswith("2") and failing:
                await asyncio.sleep(.15)  # Let the first result checkpoint commit.
                raise ValueError("第二张图暂未通过人工确认")
            return await super().analyze(image,context)
    async def parser(folder,timeout):
        parses.append(folder)
        await local_parser(folder,timeout)
    a,b = [base64.b64encode(png(c)).decode() for c in ("navy","maroon")]
    content = f'<p>织纹A<img src="data:image/png;base64,{a}"></p><p>织纹B<img src="data:image/png;base64,{b}"></p>'.encode()
    runner = IngestionRunner(store,index,configuration(store.root),Vision,parser)
    doc_id = enqueue(runner.queue,"two.html",content,True)
    await runner.execute(runner.queue.claim())
    assert store.document(doc_id)["status"]=="error"
    assert not store.chunks(doc_id)
    failing = False
    runner.queue.enqueue(doc_id)
    await runner.execute(runner.queue.claim())
    assert store.document(doc_id)["status"]=="ready"
    assert len(parses)==1 and len(calls)==3
    assert store.document(doc_id)["retained_images"]==2
    assert all(c["images"] for c in store.chunks(doc_id))


async def test_failed_reindex_keeps_previous_version_searchable(storage,sample_root,monkeypatch):
    store,index = storage
    doc_id = load_doc(storage,sample_root/"01_MES生产日报_模拟.csv")
    runner = IngestionRunner(store,index,configuration(store.root),FakeVision,local_parser)
    runner.queue.enqueue(doc_id,1,True,True)
    assert store.document(doc_id)["searchable"] and store.document(doc_id)["active_revision"]==1
    original = index.stage
    def broken(*args):
        original(*args)
        raise ValueError("模拟向量已完成、元数据发布前故障")
    monkeypatch.setattr(index,"stage",broken)
    await runner.execute(runner.queue.claim())
    assert store.document(doc_id)["status"]=="error"
    assert all(h["revision"]==1 for h in index.search(store,"定型A 不良率",[doc_id]))
    monkeypatch.setattr(index,"stage",original)
    runner.queue.enqueue(doc_id)
    await runner.execute(runner.queue.claim())
    assert store.document(doc_id)["active_revision"]==2
    assert {r["revision"] for r in index.table.to_arrow().to_pylist()}=={2}
    assert index.search(store,"定型A 不良率",[doc_id])[0]["revision"]==2


def test_retry_bound_and_permanent_error(storage):
    store,_ = storage
    queue = JobQueue(store,configuration(store.root,ingestion_max_attempts=2))
    doc_id = enqueue(queue)
    job = queue.claim()
    queue.fail(job,"网络抖动",retry=True,delay=0)
    job = queue.claim()
    assert job["attempts"]==2
    queue.fail(job,"仍然失败",retry=True,delay=0)
    assert queue.claim() is None and store.document(doc_id)["status"]=="error"


def test_mutation_compare_and_swap_and_recovery(storage,sample_root):
    store,_ = storage
    doc_id = load_doc(storage,sample_root/"01_MES生产日报_模拟.csv")
    queue = JobQueue(store,configuration(store.root))
    mutation = queue.reserve_mutation(doc_id,"editing",1)
    with pytest.raises(Conflict):
        queue.enqueue(doc_id,1,True,True)
    with pytest.raises(Conflict):
        queue.reserve_mutation(doc_id,"editing",1)
    store.execute("UPDATE mutations SET lease_until=0")
    queue.recover_mutations()
    assert store.document(doc_id)["status"]=="ready"
    with store.connect() as c, pytest.raises(Conflict):
        queue.check_mutation(c,mutation)


def test_search_uses_persistent_fts_and_candidate_reads(storage,sample_root,monkeypatch):
    store,index = storage
    doc_id = load_doc(storage,sample_root/"01_MES生产日报_模拟.csv")
    def forbidden(*args,**kwargs):
        raise AssertionError("Search must not load the complete corpus")
    monkeypatch.setattr(store,"chunks",forbidden)
    hits = index.search(store,"定型A 不良率",[doc_id])
    assert hits and all(h["document_id"]==doc_id for h in hits)


def test_embedding_cache_reuses_unchanged_content(storage,monkeypatch):
    store,index = storage
    calls = []
    original = index.embed
    def embed(texts,query=False):
        calls.extend(texts)
        return original(texts,query)
    monkeypatch.setattr(index,"embed",embed)
    first = index.cached_vectors(["棉布","棉布","经向缩水"])
    second = index.cached_vectors(["棉布","经向缩水","新工艺"])
    assert len(calls)==3 and first[0]==first[1]
    assert first[0]==pytest.approx(second[0])


def test_legacy_fts_backfill_and_media_association(storage,sample_root):
    store,index = storage
    doc_id = load_doc(storage,sample_root/"01_MES生产日报_模拟.csv")
    store.execute("DELETE FROM chunk_fts")
    store.execute("UPDATE chunks SET fts_indexed=0")
    assert not store.lexical("定型A")
    assert store.backfill_fts(3)==3
    assert store.lexical("定型A",[doc_id])
    while store.backfill_fts(3):
        pass
    assert store.query("SELECT count(*) AS n FROM chunk_fts")[0]["n"]==12


async def test_100k_backlog_uses_fixed_workers_instead_of_100k_tasks(storage):
    from backend.store import now
    store,index = storage
    runner = IngestionRunner(store,index,configuration(store.root,ingestion_queue_limit=100000),FakeVision,local_parser)
    stamp = now()
    with store.connect() as c:
        c.executemany("INSERT INTO documents(id,name,suffix,size,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ((f'{i:016x}',f'积压测试{i}.txt','.txt',1,'queued',stamp,stamp) for i in range(100000)))
        c.execute("INSERT INTO ingest_jobs(id,document_id,revision,created_at,updated_at) SELECT id,id,1,?,? FROM documents",(stamp,stamp))
    blocked = asyncio.Event()
    async def hold(job):
        await blocked.wait()
    runner.process = hold
    runner.start()
    try:
        await wait_until(lambda:runner.queue.statistics()['running']==2)
        assert len(runner.workers)==3
        assert runner.queue.statistics()['queued']==99998
        assert len(store.documents(30,99960))==30
    finally:
        await runner.stop()
    assert runner.queue.statistics()['queued']==100000


def test_query_embedding_has_priority_between_ingestion_batches():
    import threading
    from backend.retrieval import EmbeddingGate
    gate = EmbeddingGate()
    first_started, release = threading.Event(),threading.Event()
    order = []
    def work(name,query=False):
        with gate.enter(query):
            order.append(name)
            if name=='first-batch':
                first_started.set()
                assert release.wait(3)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(work,'first-batch')
        assert first_started.wait(3)
        next_batch = pool.submit(work,'next-batch')
        query = pool.submit(work,'online-query',True)
        deadline=time.monotonic()+3
        while not gate.queries and time.monotonic()<deadline:
            time.sleep(.001)
        assert gate.queries==1
        release.set()
        for future in (first,next_batch,query):
            future.result(timeout=3)
    assert order==['first-batch','online-query','next-batch']


def test_failed_checkpoint_gc_preserves_published_images(storage,sample_root):
    store,index = storage
    doc_id = load_doc(storage,sample_root/'01_MES生产日报_模拟.csv')
    runner = IngestionRunner(store,index,configuration(store.root),FakeVision,local_parser)
    runner.queue.enqueue(doc_id,1,True,True)
    job = runner.queue.claim()
    runner.queue.fail(job,'永久格式错误')
    store.execute("UPDATE ingest_jobs SET updated_at='2000-01-01T00:00:00' WHERE id=?",(job['id'],))
    folder=store.root/'jobs'/job['id'];folder.mkdir()
    (folder/'parsed.json').write_text('{}')
    assets=store.root/'assets'/doc_id;assets.mkdir()
    published,unpublished=uid(),uid()
    for image_id in (published,unpublished):
        (assets/(image_id+'.png')).write_bytes(png())
    store.execute('INSERT INTO images VALUES(?,?,?)',(published,doc_id,json.dumps({'id':published})))
    runner.cleanup_checkpoints()
    assert not folder.exists() and not (assets/(unpublished+'.png')).exists()
    assert (assets/(published+'.png')).exists()
    assert store.query('SELECT checkpoint_cleaned FROM ingest_jobs WHERE id=?',(job['id'],))[0]['checkpoint_cleaned']==1
    runner.queue.enqueue(doc_id)
    assert runner.queue.claim() is not None


def test_empty_optional_model_path_uses_default():
    assert Settings(_env_file=None,embedding_cache_dir='').embedding_cache_dir is None


def test_lancedb_maintenance_builds_real_vector_and_scalar_indices(storage):
    import numpy as np
    import pyarrow as pa
    _,index=storage
    values=np.random.default_rng(42).standard_normal((10000,512)).astype('float32')
    values/=np.linalg.norm(values,axis=1,keepdims=True)
    vector_column=pa.FixedSizeListArray.from_arrays(pa.array(values.ravel()),512)
    table=pa.Table.from_arrays([pa.array([f'{i:016x}' for i in range(10000)]),pa.array(['a'*16]*10000),pa.array([1]*10000,type=pa.int64()),vector_column],schema=index.schema)
    index.table.add(table)
    index.maintain()
    index.table.checkout_latest()
    columns={col for item in index.table.list_indices() for col in item.columns}
    assert {'document_id','vector'}<=columns
    result=index.table.search(values[0]).distance_type('cosine').limit(1).to_list()
    assert result[0]['id']=='0'*16 and result[0]['_distance']<.0001


async def test_shutdown_waits_for_pending_claim_and_releases_late_lease(storage,monkeypatch):
    import threading
    store,index=storage
    runner=IngestionRunner(store,index,configuration(store.root),FakeVision,local_parser)
    entered,proceed=threading.Event(),threading.Event()
    original=runner.queue.claim
    def delayed_claim(fast_only=False):
        entered.set()
        assert proceed.wait(3)
        return original(fast_only)
    monkeypatch.setattr(runner.queue,'claim',delayed_claim)
    runner.start()
    await wait_until(entered.is_set)
    shutdown=asyncio.create_task(runner.stop())
    await asyncio.sleep(.05)
    assert not shutdown.done()
    enqueue(runner.queue)
    proceed.set()
    await asyncio.wait_for(shutdown,5)
    assert runner.queue.statistics()['queued']==1
    assert runner.queue.statistics()['running']==0
