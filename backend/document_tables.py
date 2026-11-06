"""Whole document tables, with repeatable headers and nearby explanatory context."""
from .tabular import headers_for, cell_text


def markdown_table(headers, rows):
    def escape(v):
        return cell_text(v).replace("|", "&#124;").replace("\n", "<br>")
    return "\n".join(["| " + " | ".join(map(escape, headers)) + " |", "| " + " | ".join("---" for _ in headers) + " |",
                      *("| " + " | ".join(map(escape, row)) + " |" for row in rows)])


def table_blocks(rows, locator, header_depth=1, structure=None):
    from .ingest import Block, clean
    rows = [[clean(cell_text(v)) for v in row] for row in rows]
    indexed = [(i, row) for i, row in enumerate(rows, 1) if any(row)]
    if len(indexed)<2:
        return [Block(" | ".join(indexed[0][1]), locator)] if indexed else []
    normalized = [r for _,r in indexed]
    headers = headers_for(normalized, header_depth)
    body = [r + [""]*(len(headers)-len(r)) for r in normalized[header_depth:]]
    metadata = {"headers": headers, "rows": body, "header_rows": normalized[:header_depth], "source_rows": [i for i,_ in indexed[header_depth:]]}
    if structure:
        metadata["structure"] = structure
    return [Block(markdown_table(headers, body), locator, "document_table", metadata)]


def html_table(table, locator):
    grid, occupied, spans = [], {}, []
    header_depth = 0
    for r, row in enumerate(table.find_all("tr")):
        if row.find("th") and header_depth == r:
            header_depth += 1
        col = 0
        for cell in row.find_all(["th", "td"], recursive=False):
            while (r,col) in occupied:
                col += 1
            rs, cs = max(1,int(cell.get("rowspan",1))), max(1,int(cell.get("colspan",1)))
            if rs>1000 or cs>200 or r+rs>2000 or col+cs>200:
                raise ValueError("HTML表格跨度过大")
            value = cell.get_text(" ", strip=True)
            for y in range(r,r+rs):
                for x in range(col,col+cs):
                    occupied[y,x] = value
            spans.append({"row": r, "column": col, "rowspan": rs, "colspan": cs})
            col += cs
    if occupied:
        width = max(c for _,c in occupied)+1
        grid = [[occupied.get((r,c), "") for c in range(width)] for r in range(max(r for r,_ in occupied)+1)]
    return table_blocks(grid, locator, max(1,header_depth), {"spans": spans})


def docx_table(table, locator, rows=None):
    from docx.oxml.ns import qn
    cells = [list(row.cells) for row in table.rows]
    rows = rows if rows is not None else [[c.text for c in row] for row in cells]
    if not cells:
        return []
    offset, caption = 0, ""
    if len(cells)>2 and len(cells[0])>1 and len({c._tc for c in cells[0]})==1:
        offset, caption = 1, rows[0][0]
    depth = 1
    declared = 0
    for row in list(table.rows)[offset:]:
        if row._tr.trPr is not None and row._tr.trPr.find(qn("w:tblHeader")) is not None:
            declared += 1
        else:
            break
    depth = max(depth,declared)
    first = cells[offset]
    if len({c._tc for c in first}) < len(first):
        depth = max(depth,2)
    for col,cell in enumerate(first):
        for r in range(offset+1,len(cells)):
            if col>=len(cells[r]) or cells[r][col]._tc is not cell._tc:
                break
            depth = max(depth,r-offset+1)
    depth = min(depth,max(1,len(rows)-offset-1))
    positions = {}
    for r,row in enumerate(cells):
        for c,cell in enumerate(row):
            positions.setdefault(cell._tc,[]).append((r,c))
    spans = [{"row":min(r for r,c in coords),"column":min(c for r,c in coords),
              "rowspan":max(r for r,c in coords)-min(r for r,c in coords)+1,
              "colspan":max(c for r,c in coords)-min(c for r,c in coords)+1} for coords in positions.values() if len(coords)>1]
    blocks = table_blocks(rows[offset:],locator,depth,{"spans":spans,"caption":caption})
    for block in blocks:
        if block.kind=="document_table":
            block.fields["source_rows"] = [r+offset for r in block.fields["source_rows"]]
            if caption:
                block.text = f"表名：{caption}\n\n" + block.text
    return blocks


def contextualize(blocks):
    for i, block in enumerate(blocks):
        if block.kind != "document_table":
            continue
        before = blocks[i-1].text[-450:] if i and blocks[i-1].kind == "text" else ""
        after = blocks[i+1].text[:450] if i+1<len(blocks) and blocks[i+1].kind == "text" else ""
        # Image anchors must stay owned by exactly one block.
        from .media import ANCHOR
        before, after = ANCHOR.sub("",before), ANCHOR.sub("",after)
        block.fields.update(context_before=before, context_after=after)
        block.text = (f"关联上文：{before}\n\n" if before else "") + block.text + (f"\n\n关联下文：{after}" if after else "")
    return blocks


def table_chunks(block, size=5000):
    """Keep small tables whole; oversized ones split by rows, never character offsets."""
    from .media import ANCHOR
    if len(block.text) <= size:
        return [dict(text=block.text, locator=block.locator, kind=block.kind, fields=block.fields,
                     images=block.images, image_ids=[im["id"] for im in block.images])]
    headers, rows = block.fields["headers"], block.fields["rows"]
    prefix = block.fields.get("context_before", "")
    suffix = block.fields.get("context_after", "")
    groups, current = [], []
    for row in rows:
        if current and len(markdown_table(headers,current+[row]))>size-1000:
            groups.append(current); current=[]
        current.append(row)
    if current:
        groups.append(current)
    chunks, offset = [], 0
    for i, group in enumerate(groups):
        text = f"表格 {block.locator} · 第{i+1}/{len(groups)}组\n{prefix}\n" + markdown_table(headers,group) + "\n" + suffix
        ids = set(ANCHOR.findall(text))
        images = [im for im in block.images if im["id"] in ids]
        fields = {**block.fields, "rows": group, "source_rows": block.fields["source_rows"][offset:offset+len(group)]}
        chunks.append(dict(text=text, locator=block.locator+f" · 行组 {i+1}", kind=block.kind, fields=fields, images=images, image_ids=[im["id"] for im in images]))
        offset += len(group)
    return chunks
