"""Public assistant timeline, shared by streaming and persisted message snapshots.

Reasoning text and raw vendor payloads never enter this projection.
"""
import copy
import asyncio
import logging
from .async_utils import durable_call


class TurnState:
    def __init__(self, model=""):
        self.data = {"content":"","parts":[],"artifacts":[],"sources":[],"trace":[],
                     "model":model,"status":"in_progress","api":"responses","vision_images_used":[]}

    def part(self, part_id):
        return next((p for p in self.data["parts"] if p["id"]==part_id),None)

    def rebuild_content(self):
        self.data["content"] = "\n\n".join(p["text"] for p in self.data["parts"] if p["type"]=="text" and p["text"])

    def apply(self, event, value):
        if event in {"assistant_start","assistant_delta","assistant_message"}:
            part = self.part(value["part_id"])
            if part is None:
                part = {"type":"text","id":value["part_id"],"text":"","phase":"pending","status":"in_progress"}
                self.data["parts"].append(part)
            if event=="assistant_delta":
                part["text"] += value["delta"]
            if event=="assistant_message":
                part.update(text=value["text"],phase=value["phase"],status="completed")
            self.rebuild_content()
        elif event=="tool_start":
            call_id = value.get("call_id",f"legacy-{len(self.data['parts'])}")
            if self.part(call_id) is None:
                self.data["parts"].append({"type":"tool","id":call_id,"name":value["name"],"arguments":value["arguments"],"status":"running"})
        elif event=="tool_result":
            part = self.part(value.get("call_id"))
            if part is not None:
                part.update(status="completed" if value["ok"] else "failed",summary=value["summary"])
            self.data["trace"].append(value)
        elif event=="sources":
            sources = {s["id"]:s for s in self.data["sources"]}
            sources.update({s["id"]:s for s in value["sources"]})
            self.data["sources"] = list(sources.values())
        elif event=="artifact":
            artifact = value["artifact"]
            if not any(a["id"]==artifact["id"] for a in self.data["artifacts"]):
                self.data["artifacts"].append(artifact)
                self.data["parts"].append({"type":"artifact","id":"part-"+artifact["id"],"artifact_id":artifact["id"]})
        elif event=="model":
            self.data["model"] = value["model"]
        elif event=="vision_images":
            self.data["vision_images_used"] = value["ids"]

    def finish(self, status, error=None):
        self.data["status"] = status
        for part in self.data["parts"]:
            if part.get("status") in {"in_progress","running"}:
                part["status"] = "interrupted" if status=="interrupted" else "failed"
        if error:
            self.data["error"] = error
        self.rebuild_content()

    def snapshot(self):
        return copy.deepcopy(self.data)


class ProgressWriter:
    """Coalesce token deltas; drain old writes before writing a terminal snapshot."""
    def __init__(self, store, message_id, model, interval=.35):
        self.store,self.message_id,self.interval=store,message_id,interval
        self.turn=TurnState(model)
        self.generation,self.saved=0,-1
        self.task=None
        self.write_lock=asyncio.Lock()

    def start(self):
        self.task=asyncio.create_task(self.loop())

    def apply(self,event,value):
        self.turn.apply(event,value)
        if event!="status":
            self.generation+=1

    async def flush(self,force=False):
        async with self.write_lock:
            version=self.generation
            if not force and version==self.saved:
                return
            snapshot=self.turn.snapshot()
            await durable_call(self.store.update_message,self.message_id,snapshot)
            self.saved=version

    async def loop(self):
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.flush()
            except Exception:
                logging.getLogger("textile.chat").warning("Progress snapshot delayed; retrying on next interval")

    async def finish(self,status,result=None,error=None):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
            self.task=None
        if result:
            result={k:v for k,v in result.items() if k in {"content","parts","artifacts","sources","trace","model","status","api","vision_images_used"}}
            if "parts" in result:
                self.turn.data.update(result)
            else:
                # Compatibility for older adapters returning one final message.
                if result.get("content"):
                    self.turn.apply("assistant_message",{"part_id":"legacy-final","text":result["content"],"phase":"final"})
                self.turn.data.update({k:v for k,v in result.items() if k!="content"})
        self.turn.finish(status,error)
        self.generation+=1
        await self.flush(force=True)
        return self.turn.snapshot()
