"""DeepSeek's stateless Responses wire format and visible-text SSE handling."""
import re


class ResponseStreamError(RuntimeError):
    public_message = "模型响应未完整结束，已保留收到的内容，请重试"


def as_dict(value):
    return value if isinstance(value,dict) else value.model_dump(exclude_none=True)


def output_text(response):
    data = as_dict(response)
    return "\n\n".join(part["text"] for item in data.get("output",[]) if item.get("type")=="message"
                         for part in item.get("content",[]) if part.get("type")=="output_text")


def clean_citations(text, sources):
    invalid = set(re.findall(r"\[C:([^\]]+)\]",text))-set(sources)
    for value in invalid:
        text=text.replace(f"[C:{value}]","[来源未验证]")
    if invalid:
        text += "\n\n部分引用未能验证，请核对原始文档。"
    return text


class CitationBuffer:
    """Do not expose an incomplete or invented source link while streaming."""
    def __init__(self, sources):
        self.sources,self.pending = sources,""

    def feed(self, delta, final=False):
        text = self.pending+delta
        self.pending = ""
        if not final:
            suffix = re.search(r"\[(?:C(?::[^\]\n]*)?)?$",text)
            if suffix and len(suffix[0])<128:
                self.pending = suffix[0]
                text=text[:suffix.start()]
        return re.sub(r"\[C:([^\]]+)\]",lambda m:m[0] if m[1] in self.sources else "[来源未验证]",text)


def replay_items(output):
    """Only documented pass-back fields; never use previous_response_id."""
    result=[]
    for item in output:
        kind=item.get("type")
        if kind=="message":
            result.append({"type":"message","role":"assistant","content":[{"type":"output_text","text":p["text"]} for p in item.get("content",[]) if p.get("type")=="output_text"]})
        elif kind=="function_call":
            result.append({k:item[k] for k in ("type","call_id","name","arguments")})
        elif kind=="reasoning" and item.get("content"):
            # Transient context only. Never emit/store chain of thought.
            result.append({"type":"reasoning","content":item["content"]})
    return result


async def stream_response(client, options, emit, sources, round_number):
    completed=None
    buffers={}
    started=set()
    stream=await client.responses.create(stream=True,**options)
    async with stream:
        async for raw in stream:
            event=as_dict(raw)
            kind=event["type"]
            if kind=="response.output_text.delta":
                item_id=event["item_id"]
                part_id=f"r{round_number}:{item_id}"
                if item_id not in started:
                    started.add(item_id)
                    await emit("assistant_start",{"part_id":part_id,"round":round_number})
                buffer=buffers.setdefault(item_id,CitationBuffer(sources))
                delta=buffer.feed(event["delta"])
                if delta:
                    await emit("assistant_delta",{"part_id":part_id,"delta":delta})
            elif kind=="response.completed":
                completed=event["response"]
                break
            elif kind in {"response.failed","response.incomplete","error"}:
                raise ResponseStreamError()
    if completed is None or completed.get("status")!="completed":
        raise ResponseStreamError()
    output=completed.get("output",[])
    pending=[item for item in output if item.get("type")=="function_call"]
    messages=[item for item in output if item.get("type")=="message"]
    for i,item in enumerate(messages):
        text="\n\n".join(p["text"] for p in item.get("content",[]) if p.get("type")=="output_text")
        if not text:
            continue
        part_id=f"r{round_number}:{item['id']}"
        # Authoritative full text also supports servers omitting delta events.
        await emit("assistant_message",{"part_id":part_id,"text":clean_citations(text,sources),
                   "phase":"commentary" if pending or i<len(messages)-1 else "final"})
    return output,pending
