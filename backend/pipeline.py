"""Bounded document workers, durable checkpoints and globally limited vision calls."""
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import shutil
import sys
import sqlite3
import time
from collections import Counter
from pathlib import Path

from filelock import FileLock, Timeout as LockTimeout

from .cache import ContentCache, digest
from .checkpoints import atomic_json, load_extraction
from .ingest import split_blocks
from .jobs import JobQueue, LeaseLost, RetryableError
from .multimodal import VISION_PROMPT, VisualDecision, enrich, persist_images
from .media import ANCHOR
from .async_utils import acquire_durable, durable_call

log = logging.getLogger("textile.ingestion")


async def run_parser(folder, timeout):
    process = await asyncio.create_subprocess_exec(sys.executable,"-m","backend.parse_worker",str(folder),
        cwd=str(Path(__file__).resolve().parents[1]),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,
        creationflags=0x08000000 if os.name == "nt" else 0)
    try:
        await asyncio.wait_for(process.wait(),timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        error = folder/"parse-error.json"
        if error.exists():
            raise ValueError(json.loads(error.read_text(encoding="utf8"))["message"])
        raise RetryableError("解析工作进程异常退出，已安排重试")


class CachedVision:
    def __init__(self, runner, job, folder, analyzer):
        self.runner, self.job, self.folder, self.analyzer = runner,job,folder,analyzer
        self.model = getattr(analyzer,"model",runner.settings.deepseek_vision_model or runner.settings.deepseek_model)

    async def analyze(self, image, context):
        r = self.runner
        key = digest("vision-v1",r.settings.deepseek_base_url,self.model,VISION_PROMPT,image.preview or image.data,context,image.locator)
        path = self.folder/"decisions"/(key+".json")
        if path.exists():
            return VisualDecision.model_validate_json(await asyncio.to_thread(path.read_text,encoding="utf8"))
        cached = await asyncio.to_thread(r.cache.get_json,"vision",key)
        if cached is not None:
            decision = VisualDecision.model_validate(cached)
        else:
            async with r.resource(self.job,"vision",r.settings.vision_concurrency,r.settings.vision_requests_per_minute):
                try:
                    raw = await asyncio.wait_for(self.analyzer.analyze(image,context),r.settings.vision_timeout_seconds)
                except TimeoutError:
                    raise RetryableError("视觉分析超时，已保存成功结果并安排重试") from None
            decision = VisualDecision.model_validate(raw)
            await durable_call(r.cache.put_json,"vision",key,decision.model_dump())
        await durable_call(atomic_json,path,decision.model_dump())
        return decision


class IngestionRunner:
    def __init__(self, store, index, settings, vision_factory, parser=run_parser):
        self.store, self.index, self.settings = store,index,settings
        self.queue = JobQueue(store,settings)
        self.cache = ContentCache(store.root,settings.cache_max_mb)
        self.vision_factory, self.parser = vision_factory,parser
        self.workers = []
        self.wake = asyncio.Event()
        self.stopping = False
        (store.root/"jobs").mkdir(exist_ok=True)
        (store.root/"locks").mkdir(exist_ok=True)

    def start(self):
        if self.settings.ingestion_enabled:
            # Reserve one lane for formats which cannot invoke OCR/vision.
            # A slow remote vendor must not consume every document worker.
            self.workers = [asyncio.create_task(self.loop(fast_only=i==0 and self.settings.ingestion_workers>1),name=f"ingestion-{i}") for i in range(self.settings.ingestion_workers)]
            self.workers.append(asyncio.create_task(self.maintenance(),name="ingestion-maintenance"))

    async def stop(self):
        self.stopping = True
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers,return_exceptions=True)

    async def loop(self, fast_only=False):
        while not self.stopping:
            try:
                job = await acquire_durable(self.queue.claim,self.queue.release,fast_only)
                if job:
                    await self.execute(job)
                    continue
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(),self.settings.ingestion_poll_seconds)
                except TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Queue polling failed; worker remains alive")
                await asyncio.sleep(min(2,self.settings.ingestion_poll_seconds*4))

    @contextlib.asynccontextmanager
    async def resource(self, job, name, capacity, rpm=None):
        slot = None
        try:
            while slot is None:
                slot = await durable_call(self.queue.resource,job,name,capacity,rpm)
                if slot is None:
                    await asyncio.sleep(.2)
            yield
        finally:
            if slot is not None:
                await durable_call(self.queue.free_resource,job,name,slot)

    async def heartbeat(self, job, parent):
        while True:
            await asyncio.sleep(self.settings.ingestion_lease_seconds/3)
            if not await asyncio.to_thread(self.queue.heartbeat,job):
                state = await asyncio.to_thread(self.store.query,"SELECT state FROM ingest_jobs WHERE id=?",(job["id"],))
                if state and state[0]["state"] == "done":
                    return
                parent.cancel()
                return

    async def execute(self, job):
        lock = FileLock(str(self.store.root/"locks"/(job["id"]+".lock")))
        doc_lock = FileLock(str(self.store.root/"locks"/("doc-"+job["document_id"]+".lock")))
        heartbeat = asyncio.create_task(self.heartbeat(job,asyncio.current_task()))
        acquired = False
        doc_acquired = False
        try:
            # A stale worker may be finishing a native write. Keep its job fenced,
            # and wait without blocking this process's event loop.
            while not acquired:
                try:
                    lock.acquire(timeout=0)
                    acquired = True
                except LockTimeout:
                    await asyncio.sleep(.2)
            while not doc_acquired:
                try:
                    doc_lock.acquire(timeout=0)
                    doc_acquired = True
                except LockTimeout:
                    await asyncio.sleep(.2)
            await self.process(job)
        except asyncio.CancelledError:
            await durable_call(self.queue.release,job)
            if self.stopping:
                raise
            # Lease loss cancels this attempt, not the fixed worker loop.
        except LeaseLost:
            log.warning("Stale job stopped: %s",job["id"])
        except Exception as exc:
            retry = isinstance(exc,(RetryableError,TimeoutError,OSError,LockTimeout,sqlite3.OperationalError))
            message = str(exc)[:600] if isinstance(exc,(ValueError,RetryableError,TimeoutError)) else "入库阶段失败，请重试或查看服务日志"
            log.error("Ingestion attempt failed job=%s stage=%s type=%s",job["id"],job["stage"],type(exc).__name__)
            delay = max(min(60,2**job["attempts"]),getattr(exc,"retry_after",None) or 0)+random.uniform(0,.5)
            await durable_call(self.queue.fail,job,message,retry,delay)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat,return_exceptions=True)
            if doc_acquired:
                doc_lock.release()
            if acquired:
                lock.release()

    async def process(self, job):
        doc = await asyncio.to_thread(self.store.document,job["document_id"])
        if not doc:
            raise LeaseLost("文档已删除")
        folder = self.store.root/"jobs"/job["id"]
        folder.mkdir(exist_ok=True)
        path = self.store.root/"uploads"/(doc["id"]+doc["suffix"])
        def checksum():
            with path.open("rb") as stream:
                return hashlib.file_digest(stream,"sha256").hexdigest()
        content_hash = await asyncio.to_thread(checksum)
        if doc["content_hash"] and content_hash != doc["content_hash"]:
            raise ValueError("原文件校验和发生变化，已停止处理")
        source = Path(__file__).parent
        parser_version = digest(*( (source/name).read_bytes() for name in ("ingest.py","pdf_layout.py","multimodal.py","media.py","parse_worker.py","checkpoints.py")),(source.parent/"uv.lock").read_bytes(),sys.version_info[:2])
        fingerprint = digest(content_hash,parser_version,doc["multimodal"],self.settings.max_multimodal_images)
        await durable_call(self.queue.stage,job,"parsing",10)
        result = await durable_call(load_extraction,folder,fingerprint)
        if result is None:
            await durable_call(atomic_json,folder/"request.json",{"path":str(path),"suffix":doc["suffix"],"multimodal":doc["multimodal"],"max_images":self.settings.max_multimodal_images,"fingerprint":fingerprint,"parent_pid":os.getpid(),"timeout":self.settings.parse_timeout_seconds})
            async with self.resource(job,"parser",self.settings.ingestion_workers):
                await self.parser(folder,self.settings.parse_timeout_seconds)
            result = await durable_call(load_extraction,folder,fingerprint)
            if result is None:
                raise RetryableError("解析检查点缺失，已安排重试")
        if result.image_assets:
            if not self.settings.deepseek_api_key:
                raise ValueError("多模态入库需要后端DEEPSEEK_API_KEY；未调用视觉模型或丢弃图片")
            await durable_call(self.queue.stage,job,"analyzing",20,image_count=len(result.image_assets))
            analyzer = CachedVision(self,job,folder,self.vision_factory())
            async def progress(done,total):
                await durable_call(self.queue.stage,job,"analyzing",20+int(30*done/total),image_count=total)
            await enrich(result,analyzer,progress)
        assets = await durable_call(persist_images,result,self.store.root,doc["id"],job["revision"])
        chunks = await durable_call(split_blocks,result.blocks)
        if not chunks:
            raise ValueError("未提取到可检索内容；纯展示图片请启用多模态入库")
        ids = set()
        for ordinal,chunk in enumerate(chunks):
            chunk.update(id=digest(job["id"],ordinal)[:16],revision=job["revision"])
            if chunk["id"] in ids:
                raise ValueError("分块ID重复")
            ids.add(chunk["id"])
            if any(i not in {im["id"] for im in chunk.get("images",[])} for i in chunk.get("image_ids",[])):
                raise ValueError("分块图片关联不完整，已停止发布")
        if assets:
            expected = Counter(im["id"] for im in assets if im["decision"]=="retain")
            actual = Counter(i for c in chunks for i in c.get("image_ids",[]))
            anchors = Counter(i for c in chunks for i in ANCHOR.findall(c["text"]))
            if actual != expected or anchors != expected:
                raise ValueError("保留图片点位缺失、重复或归属异常，已停止发布")
        prepared = await durable_call(self.store.prepare_chunks,chunks)
        await durable_call(self.queue.stage,job,"indexing",55)
        # Staged vectors do not replace the active revision. Only publish after
        # all vector batches and metadata preparation have succeeded.
        await durable_call(self.index.stage,doc["id"],chunks,job["revision"])
        await durable_call(self.queue.publish,job,chunks,prepared,assets,result)
        # Old versions are safe to delete after the metadata pointer is committed.
        # Failure here is maintenance debt, not a failed published document.
        try:
            await durable_call(self.index.cleanup,doc["id"],job["revision"])
        except Exception:
            log.warning("Deferred old vector cleanup for %s",doc["id"])

    async def maintenance(self):
        while True:
            try:
                # Incremental legacy FTS migration; no full-corpus load.
                count = await durable_call(self.store.backfill_fts)
                if count:
                    await asyncio.sleep(.05)
                    continue
                await durable_call(self.cache.prune)
                await durable_call(self.queue.recover_mutations)
                await durable_call(self.cleanup_checkpoints)
                await durable_call(self.cleanup_garbage)
                await durable_call(self.index.maintain)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Maintenance failed; will retry")
            await asyncio.sleep(60)

    def cleanup_garbage(self):
        for row in self.store.query("SELECT document_id,suffix FROM garbage LIMIT 100"):
            doc_id = row["document_id"]
            self.index.delete(doc_id)
            root = (self.store.root/"assets").resolve()
            folder = (root/doc_id).resolve()
            if folder.parent != root:
                raise ValueError("Invalid asset folder")
            if folder.exists():
                shutil.rmtree(folder)
            path = (self.store.root/"uploads"/(doc_id+row["suffix"])).resolve()
            if path.parent != (self.store.root/"uploads").resolve():
                raise ValueError("Invalid upload path")
            path.unlink(missing_ok=True)
            self.store.execute("DELETE FROM garbage WHERE document_id=?",(doc_id,))

    def cleanup_checkpoints(self):
        cutoff = time.time()-self.settings.checkpoint_retention_hours*3600
        failed_cutoff = time.time()-self.settings.failed_checkpoint_retention_hours*3600
        done_before = time.strftime("%Y-%m-%dT%H:%M:%S",time.gmtime(cutoff))
        failed_before = time.strftime("%Y-%m-%dT%H:%M:%S",time.gmtime(failed_cutoff))
        rows = self.store.query("SELECT id,document_id,state,revision FROM ingest_jobs WHERE checkpoint_cleaned=0 AND (state='done' AND updated_at<? OR state='failed' AND updated_at<?) LIMIT 100", (done_before,failed_before))
        root = (self.store.root/"jobs").resolve()
        for row in rows:
            folder = (root/row["id"]).resolve()
            if folder.parent == root:
                # Nonblocking file lock prevents deleting a worker's live files.
                try:
                    with FileLock(str(self.store.root/"locks"/(row["id"]+".lock")),timeout=0), FileLock(str(self.store.root/"locks"/("doc-"+row["document_id"]+".lock")),timeout=0):
                        current = self.store.query("SELECT state,updated_at,checkpoint_cleaned FROM ingest_jobs WHERE id=?",(row["id"],))
                        if not current or current[0]["state"]!=row["state"] or current[0]["checkpoint_cleaned"] or current[0]["updated_at"] >= (done_before if row["state"]=="done" else failed_before):
                            continue
                        if row["state"]=="failed":
                            doc = self.store.document(row["document_id"])
                            if doc and doc["active_revision"] != row["revision"]:
                                self.index.discard(row["document_id"],row["revision"])
                            known = {im["id"] for im in self.store.query("SELECT id FROM images WHERE document_id=?",(row["document_id"],))}
                            assets_root = (self.store.root/"assets").resolve()
                            assets = (assets_root/row["document_id"]).resolve()
                            if assets.parent == assets_root and assets.is_dir():
                                for asset in assets.glob("*.png"):
                                    if asset.name.split(".")[0].removesuffix("-preview") not in known:
                                        asset.unlink(missing_ok=True)
                        if folder.is_dir():
                            shutil.rmtree(folder)
                        self.store.execute("UPDATE ingest_jobs SET checkpoint_cleaned=1 WHERE id=? AND state IN ('done','failed')",(row["id"],))
                except LockTimeout:
                    pass
