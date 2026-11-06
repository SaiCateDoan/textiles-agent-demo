"""Spreadsheet normalization. Data rows are assets, never per-row RAG chunks."""
import csv
import io
import re
import zipfile
from datetime import date, datetime
from xml.etree import ElementTree as ET

MAX_CELLS = 5_000_000
MAX_DATA_ROWS = 500_000
TOTAL = re.compile(r"^(?:合计|总计|小计|总合计|grand total|subtotal|total)$", re.I)


def cell_text(value):
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value).strip()


def headers_for(rows, depth=1):
    width = max(map(len, rows), default=0)
    headers, used = [], set()
    for col in range(width):
        parts = list(dict.fromkeys(cell_text(row[col]) for row in rows[:depth] if col < len(row) and cell_text(row[col])))
        base = " / ".join(parts) or f"列{col+1}"
        name, n = base, 2
        while name.casefold() in used or name.casefold() == "_source_row":
            name = f"{base}_{n}"
            n += 1
        headers.append(name)
        used.add(name.casefold())
    return headers


def column_type(name, values):
    nonempty = [v for v in values if v != ""]
    # Identifiers and numeric-looking strings with leading zeroes remain strings.
    if not nonempty or re.search(r"编号|批次|代码|编码|序号|\bid\b|code", name, re.I):
        return "VARCHAR"
    if all(re.fullmatch(r"[-+]?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][-+]?\d+)?", v) for v in nonempty):
        if any("." in v or "e" in v.lower() for v in nonempty):
            return "DOUBLE"
        return "BIGINT" if all(-(2**63)<=int(v)<2**63 for v in nonempty) else "VARCHAR"
    return "VARCHAR"


def make_dataset(rows, row_numbers, sheet, name, depth=1, start_column=1, preserve_rows=()):
    headers = headers_for(rows, depth)
    records, excluded = [], []
    for physical, row in zip(row_numbers[depth:], rows[depth:]):
        values = [cell_text(v) for v in row] + [""] * (len(headers)-len(row))
        if not any(values) and physical not in preserve_rows:
            continue
        if TOTAL.fullmatch(next((v for v in values if v), "")):
            excluded.append(physical)
            continue
        records.append([physical, *values])
    if not records:
        return None
    columns = [{"name": h, "type": column_type(h, [r[i+1] for r in records])} for i, h in enumerate(headers)]
    return {"key": name, "name": name, "sheet": sheet, "range": {"start_row": row_numbers[0], "end_row": row_numbers[-1], "start_column": start_column, "end_column": start_column+len(headers)-1},
            "header_rows": depth, "columns": columns, "row_count": len(records), "excluded_total_rows": excluded,
            "rows": records, "sample": records[:3], "formula_count": 0, "missing_formula_cache": 0}


def bounded_rows(iterator):
    rows, cells = [], 0
    for row in iterator:
        cells += len(row)
        if len(rows) >= MAX_DATA_ROWS + 100 or cells > MAX_CELLS:
            raise ValueError(f"表格超过 {MAX_DATA_ROWS} 行或 {MAX_CELLS} 个单元格，请拆分文件")
        rows.append([cell_text(v) for v in row])
    return rows


def sheet_regions(rows, merges):
    """Blank separator bands identify independent regions; preserve physical coordinates."""
    bands, start = [], None
    for i in range(len(rows)+1):
        occupied = i < len(rows) and any(rows[i])
        # A fully blank row inside a vertical merge is still part of that region.
        occupied = occupied or any(r1 <= i+1 <= r2 for r1, c1, r2, c2 in merges)
        if occupied and start is None:
            start = i
        if not occupied and start is not None:
            bands.append((start, i)); start = None
    for start, end in bands:
        width = max(map(len, rows[start:end]), default=0)
        col_start = None
        for col in range(width+1):
            occupied = col < width and any(col < len(row) and row[col] for row in rows[start:end])
            occupied = occupied or (col < width and any(r1 <= start+1 <= r2 and c1 <= col+1 <= c2 for r1,c1,r2,c2 in merges))
            if occupied and col_start is None:
                col_start = col
            if not occupied and col_start is not None:
                yield start, end, col_start, col
                col_start = None


def native_charts(path):
    """Read OOXML references/caches without executing formulas or external links."""
    ns = {"c": "http://schemas.openxmlformats.org/drawingml/2006/chart", "a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    charts = []
    with zipfile.ZipFile(path) as archive:
        names = sorted(n for n in archive.namelist() if re.fullmatch(r"xl/charts/chart\d+\.xml", n))
        if len(names) > 100:
            raise ValueError("原生图表超过100个，请拆分工作簿")
        for name in names:
            root = ET.fromstring(archive.read(name))
            title = "".join(t.text or "" for t in root.findall(".//c:title//a:t", ns)) or name.rsplit("/", 1)[-1]
            series = []
            for item in root.findall(".//c:ser", ns):
                refs = [node.text for node in item.findall(".//c:f", ns) if node.text]
                caches = [{"index": p.get("idx"), "value": p.findtext("c:v", default="", namespaces=ns)} for p in item.findall(".//c:numCache/c:pt", ns)]
                label=item.findtext("c:tx/c:v",default="",namespaces=ns) or item.findtext("c:tx/c:strRef/c:strCache/c:pt/c:v",default="",namespaces=ns)
                series.append({"name":label,"references": refs, "cached_points": caches[:50], "cached_point_count": len(caches)})
            plot=root.find(".//c:plotArea",ns)
            chart_types=[node.tag.rsplit("}",1)[-1] for node in plot if node.tag.endswith("Chart")] if plot is not None else []
            axes=[]
            for tag in ("catAx","valAx","dateAx"):
                for axis in root.findall(f".//c:{tag}",ns):
                    fmt=axis.find("c:numFmt",ns)
                    axes.append({"type":tag,"title":"".join(t.text or "" for t in axis.findall(".//c:title//a:t",ns)),"number_format":fmt.get("formatCode") if fmt is not None else None})
            charts.append({"title": title, "part": name,"chart_types":chart_types,"axes":axes, "series": series, "cache_status": "未验证新鲜度；精确统计应查询引用数据，外部引用不自动读取"})
    return charts


def extract_tabular(path, suffix):
    from .ingest import Block, Extraction, check_archive, decode
    result = Extraction()
    if suffix in {".csv", ".tsv"}:
        rows = bounded_rows(csv.reader(io.StringIO(decode(path.read_bytes())), delimiter="\t" if suffix == ".tsv" else ","))
        indexed = [(i+1, r) for i, r in enumerate(rows) if any(r)]
        if indexed:
            dataset = make_dataset([r for _, r in indexed], [i for i, _ in indexed], "工作表 1", "工作表 1")
            if dataset:
                result.datasets.append(dataset)
    else:
        workbooks = []
        if suffix == ".xlsx":
            import openpyxl
            check_archive(path)
            values = openpyxl.load_workbook(path, read_only=True, data_only=True)
            formulas = openpyxl.load_workbook(path, read_only=True, data_only=False)
            try:
                with zipfile.ZipFile(path) as archive:
                    # Read merges from the sheet XML; read_only avoids materializing Cell objects.
                    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                    for ws in values:
                        rows = bounded_rows(ws.iter_rows(values_only=True))
                        merges = []
                        xml = ET.fromstring(archive.read(ws._worksheet_path))
                        for node in xml.findall("s:mergeCells/s:mergeCell", ns):
                            c1, r1, c2, r2 = openpyxl.utils.range_boundaries(node.get("ref"))
                            merges.append((r1, c1, r2, c2))
                        formula_cells, missing, number_formats = [], [], {}
                        for row in formulas[ws.title]:
                            for cell in row:
                                if cell.value is not None and cell.number_format != "General":
                                    number_formats.setdefault(cell.column,set()).add(cell.number_format)
                                    if isinstance(cell.value,(int,float)) and float(cell.value).is_integer() and re.fullmatch(r"0{2,20}",cell.number_format):
                                        rows[cell.row-1][cell.column-1] = f"{int(cell.value):0{len(cell.number_format)}d}"
                                if cell.data_type == "f":
                                    formula_cells.append((cell.row, cell.column, str(cell.value)))
                                    if not rows[cell.row-1][cell.column-1]:
                                        missing.append((cell.row, cell.column))
                        workbooks.append((ws.title, rows, merges, formula_cells, missing, number_formats))
            finally:
                values.close(); formulas.close()
            result.charts = native_charts(path)
            result.warnings.append("Excel公式保留表达式并读取缓存，不执行重算；缓存新鲜度未验证。原生图表缓存仅作原图证据。")
        else:
            import xlrd
            book = xlrd.open_workbook(path)
            for ws in book.sheets():
                workbooks.append((ws.name, bounded_rows(ws.row_values(i) for i in range(ws.nrows)), [], [], [], {}))
            result.warnings.append("旧XLS仅导入单元格值；公式、合并结构和原生图表请转换为XLSX后导入。")
        for sheet, rows, merges, formulas, missing, number_formats in workbooks:
            layout_rows = [list(row) for row in rows]
            for r,c,_ in formulas:
                layout_rows[r-1][c-1] = "公式"
            for start, end, left, right in sheet_regions(layout_rows, merges):
                region = [r[left:right] + [""]*max(0, right-len(r)) for r in rows[start:end]]
                # A merged full-width title is context, not the column header.
                title = ""
                if len(region)>2 and any(r1==r2==start+1 and c1==left+1 and c2==right for r1,c1,r2,c2 in merges):
                    title = next((v for v in region[0] if v), "")
                    region = region[1:]; start += 1
                depth = 1
                for r1,c1,r2,c2 in merges:
                    if r1 == start+1 and c1 >= left+1 and c2 <= right:
                        depth = max(depth, r2-start, 2 if c2>c1 else 1)
                depth = min(depth, max(1, len(region)-1))
                if depth==1 and region and any(re.fullmatch(r"[-+]?\d+(?:\.\d+)?|\d{4}-\d{2}-\d{2}(?:T.*)?",v) for v in region[0] if v):
                    raise ValueError(f"工作表 {sheet} 行 {start+1} 的区域首行含数值或日期，可能是空行隔开的明细或未识别表头；请移除明细内的分隔空行或补齐明确列名后导入")
                for r1,c1,r2,c2 in merges:
                    if start < r1 <= start+depth and left < c1 <= right:
                        value = rows[r1-1][c1-1]
                        for r in range(r1, min(r2, start+depth)+1):
                            for c in range(c1, min(c2,right)+1):
                                region[r-start-1][c-left-1] = value
                name = f"{sheet} · {title or '数据区域'} {start+1}:{end} · 列 {left+1}:{right}"
                dataset = make_dataset(region, list(range(start+1,end+1)), sheet, name, depth, left+1, {r for r,c in missing if left<c<=right})
                if dataset:
                    for c,column in enumerate(dataset["columns"],left+1):
                        formats=sorted(number_formats.get(c,()))[:10]
                        if formats:
                            column["excel_number_formats"]=formats
                        if any("%" in fmt for fmt in formats):
                            column["display_note"]="Excel以百分比显示；查询保存原始小数，例如0.03显示为3%，不要与pct列的百分点混淆"
                    relevant = [[r,c,f] for r,c,f in formulas if start<r<=end and left<c<=right]
                    dataset.update(formula_count=len(relevant), formulas=relevant[:200], formulas_truncated=len(relevant)>200,
                                   missing_formula_cache=sum(start<r<=end and left<c<=right for r,c in missing))
                    result.datasets.append(dataset)
                elif region:
                    result.blocks.append(Block(f"{sheet}: " + "\n".join(" | ".join(r) for r in region), f"工作表 {sheet} · 行 {start+1}:{end}"))
    for d in result.datasets:
        fields = {"dataset_key": d["key"], "columns": d["columns"]}
        summary = f"数据集：{d['name']}\n工作表：{d['sheet']}；记录数：{d['row_count']}\n字段：" + "；".join(f"{c['name']} ({c['type']})" for c in d["columns"])
        summary += "\n示例（仅用于理解，精确统计请查询完整数据集）：\n" + "\n".join(" | ".join(map(str,r)) for r in d["sample"])
        summary += "".join(f"\n{c['name']}：{c['display_note']}" for c in d["columns"] if c.get("display_note"))
        if d["excluded_total_rows"]:
            summary += f"\n已排除明确标注的合计/小计行，共{len(d['excluded_total_rows'])}行。"
        if d["formula_count"]:
            summary += f"\n含{d['formula_count']}个公式，{d['missing_formula_cache']}个缓存缺失；缓存新鲜度未验证。"
        result.blocks.append(Block(summary, f"工作表 {d['sheet']} · 行 {d['range']['start_row']}:{d['range']['end_row']}", "dataset", fields))
    for chart in result.charts:
        refs = [ref for series in chart["series"] for ref in series["references"]]
        result.blocks.append(Block(f"Excel原生图表：{chart['title']}\n数据引用："+"；".join(refs)+"\n"+chart["cache_status"], chart["part"], "native_chart", chart))
    if not result.blocks:
        raise ValueError("未提取到有效表格或文本")
    return result
