"""Revisioned Parquet assets and bounded, allowlisted analytical SQL execution."""
import json
import math
import os
import threading
from decimal import Decimal

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import sqlglot
from sqlglot import exp

from .cache import digest
from .store import uid

QUERY_GATE = threading.BoundedSemaphore(2)
QUERY_SECONDS = 10
MAX_RESULT_ROWS = 500
ALLOWED_NODES = set("Select From Table Identifier Column Star Alias TableAlias Where And Or Not EQ NEQ GT GTE LT LTE Is Null Boolean Literal Paren Add Sub Mul Div Mod Neg Order Ordered Group Having Limit Offset Distinct Join Subquery In Between Like ILike Case If Cast TryCast DataType DataTypeParam Interval Window WindowSpec Tuple Filter".split())
ALLOWED_FUNCTIONS = set("SUM AVG COUNT MIN MAX ROUND ABS COALESCE NULLIF LOWER UPPER TRIM LENGTH SUBSTRING REPLACE EXTRACT DATE_TRUNC STRFTIME CAST TRY_CAST STDDEV_SAMP STDDEV_POP VAR_SAMP VAR_POP CORR ROW_NUMBER RANK DENSE_RANK LAG LEAD".split())


def stage_datasets(store, doc_id, revision, extraction, chunks):
    if not extraction.datasets:
        return []
    folder = store.root / "datasets" / doc_id / str(revision)
    folder.mkdir(parents=True, exist_ok=True)
    staged = []
    for d in extraction.datasets:
        dataset_id = digest(doc_id, revision, d["key"])[:16]
        arrays = {"_source_row": pa.array([r[0] for r in d["rows"]], type=pa.int64())}
        for i, column in enumerate(d["columns"], 1):
            values = [r[i] or None for r in d["rows"]]
            if column["type"] == "BIGINT":
                values = [int(v) if v is not None else None for v in values]
                typ = pa.int64()
            elif column["type"] == "DOUBLE":
                values = [float(v) if v is not None else None for v in values]
                if any(v is not None and not math.isfinite(v) for v in values):
                    raise ValueError("数据集中出现非有限数值，请校正原文件")
                typ = pa.float64()
            else:
                typ = pa.string()
            arrays[column["name"]] = pa.array(values, type=typ)
        path = folder / (dataset_id + ".parquet")
        temp = path.with_suffix(".tmp")
        try:
            pq.write_table(pa.table(arrays), temp, compression="zstd")
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        catalog = next(c for c in chunks if c["kind"] == "dataset" and c["fields"]["dataset_key"] == d["key"])
        catalog["fields"]["dataset_id"] = dataset_id
        catalog["text"] = f"数据集ID：{dataset_id}\n" + catalog["text"]
        metadata = {k:v for k,v in d.items() if k != "rows"}
        metadata.update(id=dataset_id, document_id=doc_id, revision=revision, source_id=catalog["id"], sql_table="data", source_row_column="_source_row")
        staged.append({"id": dataset_id, "path": str(path.relative_to(store.root)), "metadata": metadata})
    return staged


def write_datasets(connection, doc_id, revision, staged):
    connection.execute("DELETE FROM datasets WHERE document_id=?", (doc_id,))
    connection.executemany("INSERT INTO datasets(id,document_id,revision,path,metadata) VALUES(?,?,?,?,?)",
                           [(d["id"],doc_id,revision,d["path"],json.dumps(d["metadata"],ensure_ascii=False)) for d in staged])


def list_datasets(store, document_id=None, document_ids=None, offset=0, limit=50):
    clauses = ["d.revision=doc.active_revision"]
    args = []
    if document_id:
        clauses.append("d.document_id=?"); args.append(document_id)
    if document_ids is not None:
        if not document_ids:
            return []
        clauses.append("d.document_id IN ("+",".join("?" for _ in document_ids)+")"); args.extend(document_ids)
    rows = store.query("SELECT d.metadata,doc.name AS document_name FROM datasets d JOIN documents doc ON doc.id=d.document_id WHERE "+" AND ".join(clauses)+" ORDER BY d.document_id,d.id LIMIT ? OFFSET ?", (*args, min(limit,100),max(0,offset)))
    return [{**json.loads(r["metadata"]), "document_name": r["document_name"]} for r in rows]


def get_dataset(store, dataset_id, document_ids=None):
    rows = store.query("SELECT d.*,doc.name AS document_name FROM datasets d JOIN documents doc ON doc.id=d.document_id WHERE d.id=? AND d.revision=doc.active_revision", (dataset_id,))
    if not rows or (document_ids is not None and rows[0]["document_id"] not in document_ids):
        raise ValueError("数据集不存在、版本已变化或不在当前检索范围内")
    row = rows[0]
    return {**json.loads(row["metadata"]), "document_name": row["document_name"], "path": row["path"]}


def validate_sql(sql, aliases):
    if not isinstance(sql,str) or not 1 <= len(sql) <= 12000:
        raise ValueError("SQL长度须为1至12000字符")
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.ParseError as exc:
        raise ValueError("SQL语法无法解析，请检查字段和表达式") from exc
    if len(statements)!=1 or not isinstance(statements[0], exp.Select):
        raise ValueError("仅允许单条SELECT查询")
    tree = statements[0]
    for node in tree.walk():
        if isinstance(node,exp.Func):
            name = node.name.upper() if isinstance(node,exp.Anonymous) else node.sql_name()
            if name not in ALLOWED_FUNCTIONS and not isinstance(node, (exp.Case,exp.If)):
                raise ValueError(f"不支持的查询函数：{name}")
        elif type(node).__name__ not in ALLOWED_NODES:
            raise ValueError(f"不支持的SQL结构：{type(node).__name__}")
        if isinstance(node,exp.Table):
            if not isinstance(node.this,exp.Identifier) or node.name not in aliases or node.db or node.catalog:
                raise ValueError("只能查询本次提供的数据集别名，不能访问文件、网络或系统表")
    if not list(tree.find_all(exp.Table)):
        raise ValueError("查询必须读取所选数据集")
    return tree.sql(dialect="duckdb")


def json_value(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v,float) and not math.isfinite(v):
        return None
    if hasattr(v,"isoformat"):
        return v.isoformat()
    return v


def query_datasets(store, dataset_ids, sql, description, document_ids=None, max_rows=MAX_RESULT_ROWS, persist=True):
    if not isinstance(dataset_ids,list) or not 1<=len(dataset_ids)<=4 or len(set(dataset_ids))!=len(dataset_ids):
        raise ValueError("请提供1至4个不同的数据集ID")
    if not isinstance(description,str) or not description.strip() or len(description)>2000:
        raise ValueError("请说明筛选范围、单位和计算口径（最多2000字）")
    datasets = [get_dataset(store,i,document_ids) for i in dataset_ids]
    aliases = ["data"] if len(datasets)==1 else [f"data{i+1}" for i in range(len(datasets))]
    normalized = validate_sql(sql,aliases)
    max_rows = max(1,min(MAX_RESULT_ROWS,int(max_rows)))
    if not QUERY_GATE.acquire(timeout=1):
        raise ValueError("分析任务繁忙，请稍后重试")
    conn, timer = None, None
    try:
        conn = duckdb.connect(config={"enable_external_access":"false", "autoload_known_extensions":"false", "autoinstall_known_extensions":"false", "threads":"2", "memory_limit":"512MB"})
        total_bytes = 0
        for alias,d in zip(aliases,datasets):
            path = (store.root/d["path"]).resolve()
            if not path.is_relative_to((store.root/"datasets").resolve()):
                raise ValueError("无效数据资产路径")
            parquet = pq.ParquetFile(path)
            total_bytes += sum(parquet.metadata.row_group(i).total_byte_size for i in range(parquet.metadata.num_row_groups))
            if total_bytes>256*1024*1024:
                raise ValueError("所选数据解压后超过256MB单次分析上限，请拆分数据集")
            conn.register(alias,parquet.read())
        timer = threading.Timer(QUERY_SECONDS,conn.interrupt)
        timer.daemon = True; timer.start()
        cursor = conn.execute(f"SELECT * FROM ({normalized}) AS result LIMIT {max_rows+1}")
        columns = [d[0] for d in cursor.description]
        if len(columns)!=len(set(columns)):
            raise ValueError("结果列名重复，请为表达式和同名列指定不同别名")
        fetched = cursor.fetchall()
        rows = [[json_value(v) for v in row] for row in fetched[:max_rows]]
        if len(json.dumps(rows,ensure_ascii=False))>150000:
            raise ValueError("查询结果文本过大，请减少返回列或先聚合")
    except duckdb.Error as exc:
        raise ValueError(f"查询未完成：{str(exc)[:500]}") from exc
    finally:
        if timer:
            timer.cancel(); timer.join()
        if conn:
            conn.close()
        QUERY_GATE.release()
    # Publication can race a read; never present a result as current if that happened.
    for d in datasets:
        get_dataset(store,d["id"],document_ids)
    query_id = uid()
    provenance = [{"dataset_id":d["id"],"document_id":d["document_id"],"document_name":d["document_name"],"revision":d["revision"],"sheet":d["sheet"],"range":d["range"],"row_count":d["row_count"],"source_id":d["source_id"],"excluded_total_rows":d["excluded_total_rows"],"formula_count":d["formula_count"],"missing_formula_cache":d["missing_formula_cache"]} for d in datasets]
    result = {"query_id":query_id,"columns":columns,"rows":rows,"returned_rows":len(rows),"truncated":len(fetched)>max_rows,"sql":normalized,"description":description,"datasets":provenance,
              "note":"统计针对SQL条件覆盖的完整记录；返回行数限制只作用于查询结果。空值按SQL语义处理，公式缓存新鲜度未验证。"}
    if persist:
        store.execute("INSERT INTO dataset_queries(id,payload) VALUES(?,?)",(query_id,json.dumps(result,ensure_ascii=False)))
    return result


def query_source(result):
    datasets = result["datasets"]
    return dict(id=result["query_id"], document_id=datasets[0]["document_id"], document_name="、".join(dict.fromkeys(d["document_name"] for d in datasets)),
                locator="数据查询结果 · "+"；".join(f"{d['sheet']} v{d['revision']} ({d['row_count']}条记录)" for d in datasets),
                kind="query_result", revision=datasets[0]["revision"], images=[],
                text=json.dumps(result,ensure_ascii=False,indent=2))


def data_chart(store, query_id, x, y, chart_type="bar", title="数据分析", document_ids=None):
    found = store.query("SELECT payload FROM dataset_queries WHERE id=?",(query_id,))
    if not found:
        raise ValueError("查询结果不存在，请先查询数据")
    result = json.loads(found[0]["payload"])
    for d in result["datasets"]:
        get_dataset(store,d["dataset_id"],document_ids)
    if result["truncated"]:
        raise ValueError("查询结果被截断，请先聚合或缩小范围后再制图")
    if chart_type not in {"bar","line","scatter","pie"} or x not in result["columns"] or not isinstance(y,list) or not 1<=len(y)<=8 or any(c not in result["columns"] for c in y):
        raise ValueError("请选择结果中的横轴列、1至8个数值列及支持的图形")
    if not result["rows"]:
        raise ValueError("查询没有返回数据，无法制图")
    if chart_type=="pie" and len(y)!=1:
        raise ValueError("饼图只支持一个数值列")
    xi = result["columns"].index(x)
    series = []
    for column in y:
        yi = result["columns"].index(column)
        values = [r[yi] for r in result["rows"]]
        if any(v is not None and (isinstance(v,bool) or not isinstance(v,(int,float))) for v in values):
            raise ValueError("图表数值列包含非数值，请在查询中清洗或转换")
        points = [{"name":str(r[xi]),"value":r[yi]} for r in result["rows"]] if chart_type=="pie" else [[r[xi],r[yi]] for r in result["rows"]]
        series.append({"name":column,"type":chart_type,"data":points})
    option = {"series":series,"tooltip":{"trigger":"item"},"legend":{}}
    if chart_type!="pie":
        option.update(xAxis={"type":"value" if all(isinstance(r[xi],(int,float)) for r in result["rows"]) else "category","name":x}, yAxis={"type":"value"})
    return {"type":"chart","title":str(title)[:200],"description":result["description"],"option":option,"query_id":query_id,
            "data_origin":"query_result","source_ids":[query_id],"documents":result["datasets"],"sql":result["sql"],"total_rows":sum(d["row_count"] for d in result["datasets"]),
            "_source_rows":[query_source(result)]}
