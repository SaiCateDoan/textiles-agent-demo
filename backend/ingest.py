"""Local extraction: preserve table rows, page locators and OCR provenance."""
import csv
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf as fitz
import numpy as np
from bs4 import BeautifulSoup
from charset_normalizer import from_bytes
from PIL import Image, ImageOps

from .pdf_layout import needs_ocr, page_text

SUPPORTED = {".txt", ".md", ".csv", ".tsv", ".xlsx", ".xls", ".docx", ".pptx", ".html", ".htm", ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
MAX_PAGES = 200
MAX_ROWS = 20000
MAX_CHUNKS = 30000
_ocr = None


@dataclass
class Block:
    text: str
    locator: str
    kind: str = "text"
    fields: dict = field(default_factory=dict)
    images: list[dict] = field(default_factory=list)


@dataclass
class Extraction:
    blocks: list[Block] = field(default_factory=list)
    pages: int = 1
    ocr_pages: int = 0
    warnings: list[str] = field(default_factory=list)
    image_assets: list = field(default_factory=list)


def clean(text):
    text = unicodedata.normalize("NFKC", text).replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def decode(data):
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    result = from_bytes(data).best()
    if result is None:
        raise ValueError("无法识别文本编码，请另存为 UTF-8 后重试")
    return str(result)


def ocr_image(data):
    global _ocr
    from rapidocr_onnxruntime import RapidOCR
    if _ocr is None:
        _ocr = RapidOCR(det_use_cuda=False, cls_use_cuda=False, rec_use_cuda=False, intra_op_num_threads=4, inter_op_num_threads=2)
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    image.thumbnail((2600, 3600))
    result, _ = _ocr(np.asarray(image))
    lines = result or []
    return "\n".join(x[1] for x in lines), [float(x[2]) for x in lines]


def table_blocks(rows, locator):
    rows = [[clean(str(v)) if v is not None else "" for v in row] for row in rows]
    # Keep physical row numbers when blank lines precede or interrupt a table.
    indexed = [(i, r) for i, r in enumerate(rows, 1) if any(r)]
    if len(indexed) < 2:
        return [Block(" | ".join(indexed[0][1]), locator)] if indexed else []
    headers = []
    for i, h in enumerate(indexed[0][1]):
        h = h or f"列{i+1}"
        headers.append(h if h not in headers else f"{h}_{i+1}")
    out = []
    if len(indexed) > MAX_ROWS + 1:
        raise ValueError(f"表格超过 {MAX_ROWS} 行，请按工作表或日期拆分后导入")
    for i, row in indexed[1:]:
        fields = dict(zip(headers, row + [""] * max(0, len(headers)-len(row))))
        out.append(Block("; ".join(f"{k}: {v}" for k, v in fields.items()), f"{locator} · 行 {i}", "table", fields))
    return out


def check_archive(path):
    with zipfile.ZipFile(path) as z:
        if sum(x.file_size for x in z.infolist()) > 200 * 1024 * 1024:
            raise ValueError("Office 文档解压后超过 200MB，请拆分后导入")


def extract(path: Path, suffix: str) -> Extraction:
    out = Extraction()
    if suffix in {".txt", ".md"}:
        text = decode(path.read_bytes())
        # Markdown pipe tables retain numeric structure, other paragraphs remain searchable.
        pending, plain = [], []
        def flush():
            if pending:
                out.blocks.extend(table_blocks(pending, "Markdown 表格"))
                pending.clear()
        for line in text.splitlines():
            if "|" in line and line.strip().startswith("|"):
                if not re.fullmatch(r"[| :\-]+", line.strip()):
                    pending.append([c.strip() for c in line.strip().strip("|").split("|")])
            else:
                flush()
                plain.append(line)
        flush()
        out.blocks.insert(0, Block("\n".join(plain), "正文"))
    elif suffix in {".html", ".htm"}:
        soup = BeautifulSoup(decode(path.read_bytes()), "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "noscript"]):
            tag.decompose()
        for i, table in enumerate(soup.find_all("table"), 1):
            out.blocks.extend(table_blocks([[c.get_text(" ", strip=True) for c in r.find_all(["th", "td"])] for r in table.find_all("tr")], f"HTML 表 {i}"))
            table.decompose()
        out.blocks.append(Block(soup.get_text("\n", strip=True), "HTML 正文"))
    elif suffix in {".csv", ".tsv"}:
        text = decode(path.read_bytes())
        delimiter = "\t" if suffix == ".tsv" else ","
        out.blocks = table_blocks(list(csv.reader(io.StringIO(text), delimiter=delimiter)), "工作表 1")
    elif suffix in {".xlsx", ".xls"}:
        if suffix == ".xlsx":
            import openpyxl
            check_archive(path)
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            for ws in wb:
                out.blocks.extend(table_blocks(list(ws.iter_rows(values_only=True)), f"工作表 {ws.title}"))
            wb.close()
            out.warnings.append("Excel 公式读取缓存值；未缓存的公式为空，请先用 Excel 重算保存。合并表头建议清洗为单行。")
        else:
            import xlrd
            wb = xlrd.open_workbook(path)
            for ws in wb.sheets():
                out.blocks.extend(table_blocks([ws.row_values(i) for i in range(ws.nrows)], f"工作表 {ws.name}"))
    elif suffix == ".docx":
        from docx import Document
        check_archive(path)
        doc = Document(path)
        out.blocks = [Block(p.text, f"段落 {i}") for i, p in enumerate(doc.paragraphs, 1) if p.text.strip()]
        for i, t in enumerate(doc.tables, 1):
            out.blocks.extend(table_blocks([[c.text for c in r.cells] for r in t.rows], f"Word 表 {i}"))
        with zipfile.ZipFile(path) as z:
            for f in z.namelist():
                if f.startswith("word/media/") and Path(f).suffix.lower() in SUPPORTED:
                    text, scores = ocr_image(z.read(f))
                    if text:
                        out.blocks.append(Block(text, f"内嵌图 {Path(f).name}", "ocr"))
                        out.ocr_pages += 1
    elif suffix == ".pptx":
        from pptx import Presentation
        check_archive(path)
        slides = Presentation(path).slides
        if len(slides) > MAX_PAGES:
            raise ValueError("幻灯片过多，请拆分至 200 页以内")
        out.pages = len(slides)
        for i, slide in enumerate(slides, 1):
            for shape in slide.shapes:
                if shape.has_text_frame:
                    out.blocks.append(Block(shape.text, f"幻灯片 {i}"))
                if shape.has_table:
                    out.blocks.extend(table_blocks([[c.text for c in r.cells] for r in shape.table.rows], f"幻灯片 {i} 表格"))
                if shape.shape_type == 13:
                    text, _ = ocr_image(shape.image.blob)
                    if text:
                        out.blocks.append(Block(text, f"幻灯片 {i} 图片", "ocr"))
                        out.ocr_pages += 1
    elif suffix == ".pdf":
        with fitz.open(path) as pdf:
            if pdf.is_encrypted:
                raise ValueError("PDF 已加密，请解密后导入")
            if len(pdf) > MAX_PAGES:
                raise ValueError("PDF 超过 200 页，请拆分后导入")
            out.pages = len(pdf)
            for i, page in enumerate(pdf, 1):
                text = page_text(page)
                locator = f"第 {i} 页"
                if needs_ocr(page, text):
                    text, scores = ocr_image(page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png"))
                    out.blocks.append(Block(text, locator + " · OCR", "ocr"))
                    out.ocr_pages += 1
                    if scores and min(scores) < .8:
                        out.warnings.append(f"第 {i} 页部分 OCR 置信度低于 80%，请核对数字和单位")
                else:
                    out.blocks.append(Block(text, locator))
                    try:
                        for j, table in enumerate(page.find_tables().tables, 1):
                            out.blocks.extend(table_blocks(table.extract(), f"{locator} 表 {j}"))
                    except Exception:
                        out.warnings.append(f"第 {i} 页表格结构未识别，保留页面文本")
                    # Mixed PDFs may contain valuable text inside images even with a text layer.
                    seen = set()
                    for image in page.get_images(full=True):
                        xref = image[0]
                        if xref in seen or image[2] < 150 or image[3] < 80:
                            continue
                        seen.add(xref)
                        data = pdf.extract_image(xref)
                        image_text, _ = ocr_image(data["image"])
                        if image_text.strip():
                            out.blocks.append(Block(image_text, f"{locator} 图片 {xref} · OCR", "ocr"))
                            out.ocr_pages += 1
    else:
        with Image.open(path) as image:
            nframes = getattr(image, "n_frames", 1)
            if nframes > MAX_PAGES:
                raise ValueError("图片超过 200 帧，请拆分后导入")
            out.pages = nframes
            for i in range(nframes):
                image.seek(i)
                buf = io.BytesIO()
                image.convert("RGB").save(buf, format="PNG")
                text, scores = ocr_image(buf.getvalue())
                out.blocks.append(Block(text, f"图片 {i+1} · OCR", "ocr"))
                out.ocr_pages += 1
                if scores and min(scores) < .8:
                    out.warnings.append("部分 OCR 置信度低于 80%，请核对识别文本")
    if out.ocr_pages:
        out.warnings.append("OCR 文本可编辑校对；无网格的扫描表格保留为文本，不自动视作精确结构化数据。")
    out.blocks = [Block(clean(b.text), b.locator, b.kind, b.fields) for b in out.blocks if clean(b.text)]
    if not out.blocks:
        raise ValueError("未提取到有效文本，图片可能模糊或空白；请检查原件")
    return out


def split_blocks(blocks, size=420, overlap=60):
    if size < 2 or not 0 <= overlap < size:
        raise ValueError("分块长度必须大于重叠长度")
    chunks = []
    for block in blocks:
        if block.images:
            from .media import split_anchored
            if block.kind == "table" or (block.kind == "visual" and len(block.images) == 1):
                if len(block.text) > 6000:
                    raise ValueError("单条表格或独立视觉描述超过6000字，请拆分后导入")
                from .media import ANCHOR
                known = {im["id"] for im in block.images}
                ids = list(dict.fromkeys(i for i in ANCHOR.findall(block.text) if i in known))
                images = [im for im in block.images if im["id"] in ids]
                chunks.append(dict(text=block.text, locator=block.locator, kind=block.kind, fields=block.fields, image_ids=[im["id"] for im in images], images=images))
            else:
                chunks.extend(split_anchored(block, size, overlap))
            continue
        if block.kind == "table":
            if len(block.text) > 6000:
                raise ValueError("单行表格文本超过 6000 字，请拆分列后导入")
            chunks.append(dict(text=block.text, locator=block.locator, kind=block.kind, fields=block.fields))
            continue
        start = 0
        while start < len(block.text):
            end = min(start + size, len(block.text))
            if end < len(block.text):
                lower = start + size//2
                paragraph = block.text.rfind("\n\n", lower, end)
                sentences = list(re.finditer(r"[。！？]|[.!?](?=\s|$)", block.text[lower:end]))
                cut = paragraph if paragraph > start else (lower + sentences[-1].end()-1 if sentences else max(block.text.rfind("\n", lower, end), block.text.rfind(" ", lower, end)))
                if cut > start:
                    end = cut + 1
            chunks.append(dict(text=block.text[start:end].strip(), locator=block.locator, kind=block.kind, fields={}))
            if end == len(block.text):
                break
            start = max(start+1, end-overlap)
            # English overlap must not begin with half of the previous word.
            while start < end and block.text[start-1:start].isascii() and block.text[start-1:start].isalnum() and block.text[start:start+1].isascii() and block.text[start:start+1].isalnum():
                start += 1
    if len(chunks) > MAX_CHUNKS:
        raise ValueError(f"分块超过 {MAX_CHUNKS}，请拆分文档")
    return chunks
