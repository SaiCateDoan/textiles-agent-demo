import asyncio
import json
import logging
import re
import shutil
import hashlib
import os
import sqlite3
import anyio
from collections import Counter
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from filelock import FileLock, Timeout as LockTimeout

from .agent import TextileAgent
from .config import settings
from .ingest import SUPPORTED
from .retrieval import VectorIndex, evaluate
from .store import Store, now, uid
from .media import ANCHOR
from .multimodal import DeepSeekVision
from .jobs import Conflict, QueueFull
from .pipeline import IngestionRunner
from .async_utils import acquire_durable, durable_call
from .turns import ProgressWriter
from .responses import ResponseStreamError
from .datasets import list_datasets, get_dataset, query_datasets

log = logging.getLogger("textile")
store = Store(settings.textile_data_dir)
index = VectorIndex(settings.textile_data_dir, settings.embedding_model, settings.onnx_provider)
agent = TextileAgent(store, index)
tasks = set()
chat_locks = {}


def track(coro):
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


@asynccontextmanager
async def document_mutation(doc_id, status, revision=None):
    queue = app.state.ingestion.queue
    mutation = await acquire_durable(queue.reserve_mutation,queue.release_mutation,doc_id,status,revision)
    lock = FileLock(str(store.root/"locks"/("doc-"+doc_id+".lock")))
    acquired = False
    async def heartbeat():
        while True:
            await asyncio.sleep(settings.ingestion_lease_seconds/3)
            if not await asyncio.to_thread(queue.renew_mutation,mutation):
                return
    pulse = asyncio.create_task(heartbeat())
    try:
        for _ in range(30):
            try:
                lock.acquire(timeout=0)
                acquired = True
                break
            except LockTimeout:
                await asyncio.sleep(.1)
        if not acquired:
            raise HTTPException(503,"该文档的上次操作仍在收尾，请稍后重试",headers={"Retry-After":"3"})
        yield mutation
    finally:
        pulse.cancel()
        await asyncio.gather(pulse,return_exceptions=True)
        if acquired:
            lock.release()
        await durable_call(queue.release_mutation,mutation)


@asynccontextmanager
async def lifespan(app):
    def vision_factory():
        # One durable request attempt == one vendor request; retries belong to
        # the job queue, so SDK retries cannot silently exceed the global budget.
        return DeepSeekVision(agent.client.with_options(max_retries=0,timeout=settings.vision_timeout_seconds),settings.deepseek_vision_model or settings.deepseek_model)
    app.state.ingestion = IngestionRunner(store,index,settings,vision_factory)
    app.state.upload_slots = asyncio.Semaphore(settings.upload_concurrency)
    app.state.ingestion.start()
    try:
        yield
    finally:
        await app.state.ingestion.stop()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await agent.client.close()


app = FastAPI(title="织知 Textile Agent API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"], allow_credentials=False, allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Content-Type","Idempotency-Key"])


@app.exception_handler(QueueFull)
async def queue_full(request, exc):
    return JSONResponse(status_code=503,content={"detail":str(exc)},headers={"Retry-After":"10"})


@app.exception_handler(Conflict)
async def conflict(request, exc):
    return JSONResponse(status_code=409,content={"detail":str(exc)})


@app.exception_handler(sqlite3.OperationalError)
async def storage_busy(request, exc):
    log.error("Metadata operation failed (%s)",type(exc).__name__)
    return JSONResponse(status_code=503,content={"detail":"元数据服务暂时繁忙，请稍后重试"},headers={"Retry-After":"3"})


@app.get("/api/health")
def health():
    return {"status": "ok", "chat_api":"responses", "model": settings.deepseek_model, "vision_model": settings.deepseek_vision_model or settings.deepseek_model, "multimodal_max_images": settings.max_multimodal_images, "api_key_configured": bool(settings.deepseek_api_key), "embedding_model": settings.embedding_model, "embedding_loaded": index._model is not None, "embedding_provider": settings.onnx_provider, "ocr": "RapidOCR Chinese / ONNX CPU", **store.statistics(), "ingestion":app.state.ingestion.queue.statistics(), "supported": sorted(SUPPORTED)}


@app.get("/api/documents")
def documents(limit: int = Query(100,ge=1,le=1000), offset: int = Query(0,ge=0), q: str = Query("",max_length=180)):
    return store.documents(limit,offset,q)


@app.get("/api/documents/page")
def document_page(limit: int = Query(30,ge=1,le=100), offset: int = Query(0,ge=0), q: str = Query("",max_length=180)):
    rows = store.documents(limit,offset,q)
    total = store.document_count(q)
    return {"items":rows,"total":total,"next_offset":offset+limit if offset+limit<total else None}


@app.post("/api/documents", status_code=202)
async def upload(file: UploadFile = File(...), multimodal: bool = Form(False), idempotency_key: str | None = Header(None,max_length=128)):
    name = (file.filename or "document").replace("\\", "/").split("/")[-1][:180]
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if suffix not in SUPPORTED:
        raise HTTPException(415, "不支持此格式；旧版 .doc/.ppt 请另存为 .docx/.pptx，支持 PDF、Office、CSV、文本、HTML 和常见图片")
    doc_id = uid()
    path = store.root / "uploads" / f"{doc_id}{suffix}"
    size = 0
    slots = app.state.upload_slots
    if slots.locked():
        await file.close()
        raise HTTPException(429,"上传通道繁忙，请稍后重试",headers={"Retry-After":"2"})
    await slots.acquire()
    accepted = False
    checksum = hashlib.sha256()
    try:
        free = await asyncio.to_thread(shutil.disk_usage,store.root)
        if free.free < (settings.minimum_free_disk_mb+settings.max_upload_mb)*1024*1024:
            raise HTTPException(507,"可用磁盘空间不足，暂时停止接收文件")
        with path.open("wb") as f:
            while data := await file.read(1024*1024):
                size += len(data)
                if size > settings.max_upload_mb*1024*1024:
                    raise HTTPException(413, f"文件超过 {settings.max_upload_mb}MB")
                checksum.update(data)
                await asyncio.to_thread(f.write,data)
            await asyncio.to_thread(f.flush)
            await asyncio.to_thread(os.fsync,f.fileno())
        if not size:
            raise HTTPException(400, "文件为空")
        actual = await durable_call(app.state.ingestion.queue.upload,{"id":doc_id,"name":name,"suffix":suffix,"size":size,"multimodal":int(multimodal),"content_hash":checksum.hexdigest()},idempotency_key)
        accepted = actual == doc_id
    finally:
        if not accepted:
            # A cancellation may arrive after the registration transaction has
            # committed but before its return value reached this coroutine.
            if not await durable_call(store.document,doc_id):
                path.unlink(missing_ok=True)
        await file.close()
        slots.release()
    app.state.ingestion.wake.set()
    return await asyncio.to_thread(store.document,actual)


def require_doc(doc_id):
    if not re.fullmatch(r"[a-f0-9]{16}", doc_id):
        raise HTTPException(404, "文档不存在")
    doc = store.document(doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    return doc


@app.get("/api/documents/{doc_id}/chunks")
def chunks(doc_id: str, limit: int = Query(100,ge=1,le=1000), offset: int = Query(0,ge=0)):
    doc = require_doc(doc_id)
    if not doc["searchable"]:
        return []
    return store.chunks(doc_id,limit,offset)


@app.get("/api/documents/{doc_id}/datasets")
def document_datasets(doc_id: str, offset: int = Query(0,ge=0)):
    require_doc(doc_id)
    return list_datasets(store,doc_id,offset=offset)


@app.get("/api/datasets/{dataset_id}/preview")
def dataset_preview(dataset_id: str, offset: int = Query(0,ge=0)):
    try:
        d = get_dataset(store,dataset_id)
        result = query_datasets(store,[dataset_id],f'SELECT * FROM data ORDER BY "_source_row" LIMIT 50 OFFSET {offset}',"数据预览；按原始行号排序",max_rows=50,persist=False)
        return {**result,"total":d["row_count"],"next_offset":offset+50 if offset+50<d["row_count"] else None}
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc


@app.get("/api/documents/{doc_id}")
def document_detail(doc_id: str):
    return require_doc(doc_id)


@app.get("/api/documents/{doc_id}/job")
def document_job(doc_id: str):
    require_doc(doc_id)
    rows = store.query("SELECT id,revision,state,stage,attempts,available_at,error,updated_at FROM ingest_jobs WHERE document_id=? ORDER BY revision DESC LIMIT 1",(doc_id,))
    return rows[0] if rows else None


@app.get("/api/documents/{doc_id}/file")
def original(doc_id: str, inline: bool = False):
    doc = require_doc(doc_id)
    return FileResponse(store.root / "uploads" / f"{doc_id}{doc['suffix']}", filename=doc["name"], content_disposition_type="inline" if inline else "attachment")


@app.get("/api/images/{image_id}")
def image_file(image_id: str, preview: bool = False):
    if not re.fullmatch(r"[a-f0-9]{16}", image_id):
        raise HTTPException(404, "图片不存在")
    im = store.image(image_id)
    if not im:
        raise HTTPException(404, "图片已移除或不存在")
    folder = store.root / "assets" / im["document_id"]
    path = folder / f"{image_id}-preview.png" if preview else folder / f"{image_id}.png"
    if not path.exists() and preview:
        path = folder / f"{image_id}.png"
    if not path.exists():
        raise HTTPException(404, "图片文件不可用")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"})


class ReprocessRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    confirm_replace: bool = False


@app.post("/api/documents/{doc_id}/reprocess", status_code=202)
async def reprocess(doc_id: str, body: ReprocessRequest):
    require_doc(doc_id)
    await durable_call(app.state.ingestion.queue.enqueue,doc_id,body.expected_revision,True,body.confirm_replace)
    app.state.ingestion.wake.set()
    return store.document(doc_id)


@app.post("/api/documents/{doc_id}/retry", status_code=202)
async def retry(doc_id: str):
    require_doc(doc_id)
    await durable_call(app.state.ingestion.queue.enqueue,doc_id)
    app.state.ingestion.wake.set()
    return {"status": "queued"}


@app.delete("/api/documents/{doc_id}")
async def delete(doc_id: str):
    require_doc(doc_id)
    async with document_mutation(doc_id,"deleting") as mutation:
        await durable_call(app.state.ingestion.queue.delete_document,mutation)
        try:
            await durable_call(app.state.ingestion.cleanup_garbage)
        except Exception:
            log.warning("Document hidden; physical cleanup scheduled: %s",doc_id)
    return {"deleted": doc_id}


class ChunkEdit(BaseModel):
    text: str = Field(min_length=1, max_length=6000)
    revision: int


@app.put("/api/chunks/{chunk_id}")
async def edit_chunk(chunk_id: str, body: ChunkEdit):
    rows = await asyncio.to_thread(store.query,"SELECT document_id FROM chunks WHERE id=?",(chunk_id,))
    if not rows:
        raise HTTPException(404,"分块不存在")
    doc_id = rows[0]["document_id"]
    async with document_mutation(doc_id,"editing",body.revision) as mutation:
        doc = require_doc(doc_id)
        current = await asyncio.to_thread(store.chunks,doc_id)
        target = next(c for c in current if c["id"] == chunk_id)
        if target["kind"] in {"dataset","native_chart"} or await asyncio.to_thread(list_datasets,store,doc_id):
            raise HTTPException(422,"数据集及图表说明由原文件生成，请修改原文件后重新导入；不能单独编辑目录使其与数据不一致")
        if target["revision"] != body.revision:
            raise HTTPException(409, "分块已被修改，请刷新后编辑")
        text = body.text.strip()
        if not text:
            raise HTTPException(422, "文本不能为空")
        if target["image_ids"] and Counter(ANCHOR.findall(text)) != Counter(target["image_ids"]):
            raise HTTPException(422, "请保留该分块的全部图片位置标记，不能拆分、删除或引入其他图片ID")
        target["text"] = text
        if target["kind"] == "document_table":
            lines = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
            values = [[cell.strip().replace("&#124;","|").replace("<br>","\n") for cell in line.strip("|").split("|")] for line in lines]
            if len(values)<3 or values[0]!=target["fields"]["headers"] or any(len(r)!=len(values[0]) for r in values[2:]) or len(values[2:])!=len(target["fields"]["rows"]):
                raise HTTPException(422,"请保留Markdown表格表头、列数和数据行数，仅校正单元格内容")
            target["fields"]["rows"] = values[2:]
        if target["kind"] == "table":
            # Editable row syntax mirrors ingestion and changes downstream chart values as well.
            fields = {}
            for item in re.split(r"[;；\n]", text):
                if not item.strip():
                    continue
                if item.strip().startswith("[附图]") and target["image_ids"]:
                    if ANCHOR.sub("", item.strip()[4:]).strip():
                        raise HTTPException(422, "附图行只允许保留原有图片位置标记")
                    continue
                kv = re.split(r"[:：]", item, maxsplit=1)
                if len(kv) != 2:
                    raise HTTPException(422, "表格行请使用 列名: 值; 列名: 值 格式")
                key = kv[0].strip()
                if key in fields:
                    raise HTTPException(422, "表格行包含重复列名")
                fields[key] = kv[1].strip()
            if set(fields) != set(target["fields"]):
                raise HTTPException(422, "编辑表格行时请保留全部原始列名和单位")
            target["fields"] = fields
        revision = doc["revision"]+1
        for c in current:
            c["revision"] = revision
        try:
            prepared = await durable_call(store.prepare_chunks,current)
            await durable_call(index.stage,doc_id,current,revision)
            await durable_call(app.state.ingestion.queue.publish_edit,mutation,current,prepared,revision)
        except Exception:
            raise HTTPException(503,"索引更新失败，已保留上一版本，请重试") from None
        try:
            await durable_call(index.cleanup,doc_id,revision)
        except Exception:
            log.warning("Deferred vector cleanup after edit: %s",doc_id)
        return (await asyncio.to_thread(store.chunks_by_ids,[chunk_id]))[0]


@app.get("/api/chats")
def chats():
    return store.query("SELECT * FROM chats ORDER BY created_at DESC")


@app.post("/api/chats", status_code=201)
def create_chat():
    chat_id = uid()
    store.execute("INSERT INTO chats VALUES(?,?,?)", (chat_id, "新对话", now()))
    return {"id": chat_id, "title": "新对话"}


@app.get("/api/chats/{chat_id}/messages")
def messages(chat_id: str):
    return store.messages(chat_id)


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=6000)
    document_ids: list[str] = Field(default_factory=list, max_length=50)


@app.post("/api/chats/{chat_id}/stream")
async def chat_stream(chat_id: str, body: ChatRequest):
    if not store.query("SELECT id FROM chats WHERE id=?", (chat_id,)):
        raise HTTPException(404, "对话不存在")
    if not settings.deepseek_api_key:
        raise HTTPException(503, "后端未配置 DEEPSEEK_API_KEY，请设置环境变量并重启后端")
    if not body.content.strip():
        raise HTTPException(422, "消息不能为空")
    for doc_id in body.document_ids:
        doc = require_doc(doc_id)
        if not doc["searchable"]:
            raise HTTPException(409, "选定文档尚未就绪")
    lock = chat_locks.setdefault(chat_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "此对话正在生成，请稍后重试")
    await lock.acquire()
    queue = asyncio.Queue(maxsize=128)
    message_id=uid()
    progress=ProgressWriter(store,message_id,settings.deepseek_model)
    lock_owned=True

    def release_lock():
        nonlocal lock_owned
        if lock_owned:
            lock.release()
            lock_owned=False
            if chat_locks.get(chat_id) is lock:
                chat_locks.pop(chat_id,None)

    async def emit(event, data):
        progress.apply(event,data)
        await queue.put((event, data))

    async def work():
        try:
            await durable_call(store.begin_turn,chat_id,body.content,message_id,progress.turn.snapshot())
            progress.start()
            await emit("turn_start",{"message_id":message_id})
            result = await asyncio.wait_for(agent.run(chat_id, body.content, emit, body.document_ids or None), timeout=settings.chat_timeout_seconds)
            final = await progress.finish("completed",result)
            await emit("result",{"id":message_id,**final})
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                await progress.finish("interrupted")
            raise
        except Exception as exc:
            # Never forward vendor exceptions / request headers / secrets to the browser.
            name = type(exc).__name__
            log.error("Chat failed (%s)", name)
            message = "模型请求超时，已保留收到的内容，请稍后重试" if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else (exc.public_message if isinstance(exc,ResponseStreamError) else "DeepSeek 请求失败，已保留收到的内容，请检查配置或稍后重试")
            try:
                snapshot=await progress.finish("failed",error=message)
                await emit("turn_snapshot",{"id":message_id,**snapshot})
            except Exception:
                log.error("Failed to persist partial assistant turn")
            await emit("error", {"message": message})
        finally:
            release_lock()
            # A disconnected client cannot drain the bounded queue. Never wait
            # for a terminal event while handling its cancellation.
            if not asyncio.current_task().cancelling():
                await emit("done", {})

    task = track(work())

    async def events():
        try:
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    if event == "done":
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            # Starlette cancels its streaming task group on disconnect. Shield
            # the cleanup from that level-triggered cancellation, otherwise a
            # second cancellation can kill the partial-message DB write.
            with anyio.CancelScope(shield=True):
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                release_lock()
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ScenarioRequest(BaseModel):
    document_id: str
    parameter: str
    metric: str
    value: float
    direction: str = "min"
    revision: int


@app.post("/api/scenarios/evaluate")
async def scenario_evaluate(body: ScenarioRequest):
    doc = require_doc(body.document_id)
    if (doc["active_revision"] or doc["revision"]) != body.revision:
        raise HTTPException(409, "文档已修改，请重新生成控件以使用最新数据")
    try:
        return await asyncio.to_thread(evaluate, store, body.document_id, body.parameter, body.metric, body.value, body.direction)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
