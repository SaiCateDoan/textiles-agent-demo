"""Trusted local JSON manifests; no pickle or user-controlled asset paths."""
import json
import os
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .ingest import Block, Extraction
from .multimodal import Candidate


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temp.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_bytes(path,json.dumps(value,ensure_ascii=False).encode())


def save_extraction(folder, extraction, fingerprint, name="parsed.json"):
    candidates = []
    for image in extraction.image_assets:
        atomic_bytes(folder / (image.id + ".png"),image.data)
        if image.preview:
            atomic_bytes(folder / (image.id + "-preview.png"),image.preview)
        candidates.append({k:getattr(image,k) for k in ("id","locator","width","height","source_type","standalone","bbox")} | {"preview":bool(image.preview),"metadata":getattr(image,"metadata",None)})
    atomic_json(folder/name,{"fingerprint":fingerprint,"pages":extraction.pages,"ocr_pages":extraction.ocr_pages,"warnings":extraction.warnings,"blocks":[asdict(b) for b in extraction.blocks],"images":candidates})


def load_extraction(folder, fingerprint, name="parsed.json"):
    path = folder/name
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf8"))
    if value.get("fingerprint") != fingerprint:
        return None
    result = Extraction(blocks=[Block(**b) for b in value["blocks"]],pages=value["pages"],ocr_pages=value["ocr_pages"],warnings=value["warnings"])
    import re
    for item in value["images"]:
        if not re.fullmatch(r"[a-f0-9]{16}",item["id"]):
            raise ValueError("无效检查点图片ID")
        fields = {k:item[k] for k in ("id","locator","width","height","source_type","standalone","bbox")}
        image = Candidate(**fields,data=(folder/(item["id"]+".png")).read_bytes(),preview=(folder/(item["id"]+"-preview.png")).read_bytes() if item["preview"] else None)
        if item.get("metadata"):
            image.metadata = item["metadata"]
        result.image_assets.append(image)
    return result
