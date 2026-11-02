"""Image anchors are one logical splitting unit; they are not tokenizer vocabulary."""
import base64
import io
import re

from PIL import Image, ImageOps

ANCHOR = re.compile(r"\[\[IMG:([a-f0-9]{16})\]\]")


def marker(image_id):
    return f"[[IMG:{image_id}]]"


def normalized_png(data):
    with Image.open(io.BytesIO(data)) as source:
        if source.width * source.height > 40_000_000:
            raise ValueError("多模态图片超过4000万像素，请缩小后导入")
        im = ImageOps.exif_transpose(source)
        im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
        result = io.BytesIO()
        im.save(result, format="PNG")
        return result.getvalue(), im.width, im.height


def image_data_url(data):
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((2048, 2048))
        result = io.BytesIO()
        im.save(result, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(result.getvalue()).decode("ascii")


def retrieval_text(chunk):
    """Opaque IDs never become semantic features; image descriptions do."""
    text = ANCHOR.sub("", chunk["text"])
    descriptions = [f"图片视觉描述（模型生成）: {im['description']}" + (f"\n图片可读文字（模型转写）: {im['transcription']}" if im.get("transcription") else "") for im in chunk.get("images", [])]
    return text + ("\n" + "\n".join(descriptions) if descriptions else "")


def split_anchored(block, size, overlap):
    known = {im["id"]: im for im in block.images}
    atoms, cursor = [], 0
    for match in ANCHOR.finditer(block.text):
        if match[1] not in known:
            continue
        atoms.extend(block.text[cursor:match.start()])
        atoms.append(match[0])
        cursor = match.end()
    atoms.extend(block.text[cursor:])
    logical = "".join("\ufffc" if len(atom) > 1 else atom for atom in atoms)
    chunks, start = [], 0
    while start < len(atoms):
        end = min(start + size, len(atoms))
        if end < len(atoms):
            lower = start + size//2
            paragraph = logical.rfind("\n\n", lower, end)
            sentences = list(re.finditer(r"[。！？]|[.!?](?=\s|$)", logical[lower:end]))
            cut = paragraph if paragraph > start else (lower + sentences[-1].end()-1 if sentences else max(logical.rfind("\n", lower, end), logical.rfind(" ", lower, end)))
            if cut > start:
                end = cut + 1
        text = "".join(atoms[start:end]).strip()
        ids = list(dict.fromkeys(m[1] for m in ANCHOR.finditer(text) if m[1] in known))
        if text:
            chunks.append(dict(text=text, locator=block.locator, kind=block.kind, fields=block.fields,
                               image_ids=ids, images=[known[i] for i in ids]))
        if end == len(atoms):
            break
        next_start = max(start+1, end-overlap)
        # Keep a single owner for each picture, even across text-overlap windows.
        for i in range(next_start, end):
            if len(atoms[i]) > 1:
                next_start = i+1
        while next_start < end and logical[next_start-1:next_start].isascii() and logical[next_start-1:next_start].isalnum() and logical[next_start:next_start+1].isascii() and logical[next_start:next_start+1].isalnum():
            next_start += 1
        start = next_start
    return chunks
