"""Recover text in displayed page coordinates, retaining MuPDF's line/span hierarchy.

In particular, sorting individual words in unrotated coordinates destroys text on
landscape slides whose content is rotated relative to their PDF media box.
This is a lightweight block reader, not a general document-layout model.
"""
import re
import unicodedata
from statistics import median

import pymupdf as fitz


def _join(parts):
    out = ""
    for part in parts:
        part = unicodedata.normalize("NFKC", part).strip()
        if not part:
            continue
        # Do not insert spaces between adjacent CJK characters or before punctuation.
        contiguous = out and re.search(r"[\u3400-\u9fff]$", out) and re.match(r"[\u3400-\u9fff，。；：！？、]", part)
        separator = "" if not out or contiguous or re.match(r"[,.;:!?%)\]，。；：！？、]", part) else " "
        out += separator + part
    return out


def page_blocks(page, exclude=()):
    """Group fragments within blocks; rotate geometry BEFORE deciding reading order."""
    data = page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT, sort=False)
    matrix = page.rotation_matrix
    blocks = []
    for block in data["blocks"]:
        if block["type"] != 0:
            continue
        lines = []
        for line in block.get("lines", []):
            raw_box = fitz.Rect(line["bbox"])
            if any((raw_box & fitz.Rect(box)).get_area() >= raw_box.get_area()*.5 for box in exclude):
                continue
            # Span order is part of the source text line, not a word-level XY sort.
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                box = fitz.Rect(line["bbox"]) * matrix
                lines.append((box, text))
        if not lines:
            continue
        tolerance = max(2, median(b.height for b, _ in lines) * .3)
        rows = []
        for box, text in sorted(lines, key=lambda item: (item[0].y0, item[0].x0)):
            if rows and abs(box.y0 - rows[-1][0]) <= tolerance:
                rows[-1][1].append((box.x0, text))
            else:
                rows.append([box.y0, [(box.x0, text)]])
        paragraphs = []
        for _, fragments in rows:
            text = _join(t for _, t in sorted(fragments))
            text = re.sub(r"^[•●▪◦]\s*", "- ", text)
            if not paragraphs or re.match(r"^(?:[-•●▪◦]\s|\d+[.)]\s|[a-z][)]\s)", text):
                paragraphs.append(text)
            else:
                paragraphs[-1] = _join([paragraphs[-1], text])
        box = fitz.Rect(block["bbox"]) * matrix
        blocks.append((box, "\n".join(paragraphs)))
    # Preserve complete blocks so neighbouring columns are never interleaved word by word.
    blocks.sort(key=lambda item: (round(item[0].y0, 1), item[0].x0))
    return blocks


def page_text(page):
    return "\n\n".join(text for _, text in page_blocks(page))


def needs_ocr(page, text):
    chars = re.sub(r"\s", "", text)
    if not chars or chars.count("\ufffd") / max(1, len(chars)) > .02:
        return True
    if len(chars) >= 80:
        return False
    # A short digital title is valid text. A page number over a full-page scan is not.
    for image in page.get_images(full=True):
        for rect in page.get_image_rects(image[0]):
            if rect.get_area() >= page.rect.get_area() * .6:
                return True
    return False
