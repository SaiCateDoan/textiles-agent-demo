import json
from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference

from backend.datasets import list_datasets, query_datasets, data_chart, get_dataset
from backend.ingest import extract, split_blocks
from backend.checkpoints import save_extraction, load_extraction
from conftest import load_doc


def csv_doc(storage,tmp_path,body,name="data.csv"):
    path=tmp_path/name; path.write_text(body,encoding="utf8")
    return load_doc(storage,path)


def test_large_csv_has_catalog_not_row_embeddings_and_exact_aggregate(storage,tmp_path):
    doc=csv_doc(storage,tmp_path,"批次,机台,不良数,检验数\n"+"".join(f"{i:06d},A,{i%3},10\n" for i in range(25001)))
    store,index=storage
    assert len(store.chunks(doc))==1 and index.table.count_rows()==1
    d=list_datasets(store,doc)[0]
    assert d["row_count"]==25001 and d["columns"][0]["type"]=="VARCHAR"
    result=query_datasets(store,[d["id"]],'SELECT COUNT(*) AS n, SUM("不良数") AS defects, SUM("不良数") * 100.0 / SUM("检验数") AS rate FROM data',"全表加权不良率，单位%")
    assert result["rows"][0][0]==25001
    assert result["rows"][0][1]==sum(i%3 for i in range(25001))
    assert not result["truncated"]
    exact=query_datasets(store,[d["id"]],'''SELECT "批次", "_source_row" FROM data WHERE "批次"='024999' ''',"精确批次查询")
    assert exact["rows"]==[["024999",25001]]


def test_nulls_weighting_totals_and_chart_provenance(storage,tmp_path):
    doc=csv_doc(storage,tmp_path,"机台,不良数,检验数\nA,1,10\nA,9,990\nB,,10\n合计,10,1010\n")
    store,_=storage; d=list_datasets(store,doc)[0]
    assert d["excluded_total_rows"]==[5] and d["row_count"]==3
    r=query_datasets(store,[d["id"]],'SELECT "机台", SUM("不良数")*100.0/NULLIF(SUM("检验数"),0) AS "不良率_pct" FROM data GROUP BY "机台" ORDER BY "机台"',"按机台汇总：不良数合计/检验数合计×100；空值不当零")
    assert r["rows"]==[["A",1.0],["B",None]]
    a=data_chart(store,r["query_id"],"机台",["不良率_pct"])
    assert a["option"]["series"][0]["data"]==r["rows"]
    assert a["source_ids"]==[r["query_id"]] and a["data_origin"]=="query_result"
    assert json.loads(a["_source_rows"][0]["text"])["sql"]==r["sql"]


@pytest.mark.parametrize("sql",[
    "DELETE FROM data", "SELECT * FROM data; SELECT * FROM data", "COPY data TO 'out.csv'",
    "SELECT * FROM read_csv('C:/secret.csv')", "SELECT * FROM read_parquet('https://example.org/file')",
    "SELECT * FROM sqlite_master", "SELECT * FROM information_schema.tables", "SELECT getenv('PATH') FROM data",
    "ATTACH 'other.db' AS other", "SELECT * FROM query('SELECT * FROM data')", "SELECT * INTO other FROM data",
    "WITH RECURSIVE x AS (SELECT 1) SELECT * FROM x", "SELECT repeat('x',999999999) FROM data",
])
def test_sql_is_read_only_and_cannot_escape_assets(storage,tmp_path,sql):
    doc=csv_doc(storage,tmp_path,"x,y\n1,2\n")
    store,_=storage; d=list_datasets(store,doc)[0]
    with pytest.raises(ValueError):
        query_datasets(store,[d["id"]],sql,"测试查询")
    assert get_dataset(store,d["id"])["row_count"]==1


def test_scope_join_truncation_and_stale_results(storage,tmp_path):
    a=csv_doc(storage,tmp_path,"批次,数量\nA,10\nB,20\n", "a.csv")
    b=csv_doc(storage,tmp_path,"批次,机台\nA,M1\nB,M2\n", "b.csv")
    store,_=storage; da=list_datasets(store,a)[0];db=list_datasets(store,b)[0]
    with pytest.raises(ValueError,match="范围"):
        query_datasets(store,[db["id"]],"SELECT * FROM data","查询",document_ids=[a])
    r=query_datasets(store,[da["id"],db["id"]],'SELECT b."机台", a."数量" FROM data1 a JOIN data2 b ON a."批次"=b."批次" ORDER BY b."机台"',"按唯一批次关联")
    assert r["rows"]==[["M1",10],["M2",20]] and len(r["datasets"])==2
    short=query_datasets(store,[da["id"]],"SELECT * FROM data","明细预览",max_rows=1)
    assert short["truncated"]
    with pytest.raises(ValueError,match="截断"):
        data_chart(store,short["query_id"],"批次",["数量"])
    store.update_doc(a,active_revision=2)
    with pytest.raises(ValueError,match="版本"):
        data_chart(store,r["query_id"],"机台",["数量"])
    assert json.loads(store.query("SELECT payload FROM dataset_queries WHERE id=?",(r["query_id"],))[0]["payload"])["rows"]==r["rows"]


def test_excel_regions_merged_headers_formula_and_native_chart(tmp_path):
    wb=Workbook(); ws=wb.active; ws.title="试验"
    ws.merge_cells("A1:C1");ws["A1"]="生产报表"
    ws.merge_cells("A2:A3");ws["A2"]="机台"
    ws.merge_cells("B2:C2");ws["B2"]="质量"
    ws["B3"]="不良数";ws["C3"]="检验数"
    ws.append(["A",1,100]);ws.append(["B",2,100]);ws.append(["合计",3,200])
    ws.append([]);ws.append(["温度","缩水率"]);ws.append([160,3.5]);ws.append([170,"=3+0.1"])
    chart=BarChart();chart.title="机台不良数";chart.add_data(Reference(ws,min_col=2,min_row=3,max_row=5),titles_from_data=True);ws.add_chart(chart,"E1")
    path=tmp_path/"report.xlsx";wb.save(path)
    e=extract(path,".xlsx")
    assert len(e.datasets)==2
    assert [c["name"] for c in e.datasets[0]["columns"]]==["机台","质量 / 不良数","质量 / 检验数"]
    assert e.datasets[0]["excluded_total_rows"]==[6]
    assert e.datasets[1]["formula_count"]==e.datasets[1]["missing_formula_cache"]==1
    assert e.charts[0]["title"]=="机台不良数" and any("$B$4:$B$5" in ref for s in e.charts[0]["series"] for ref in s["references"])
    save_extraction(tmp_path,e,"test")
    restored=load_extraction(tmp_path,"test")
    assert restored.datasets==e.datasets and restored.charts==e.charts


def test_document_tables_keep_context_headers_and_rows(tmp_path):
    path=tmp_path/"guide.md";path.write_text("# 染整试验\n适用纯棉织物。\n\n| 温度_C | 缩水率_pct |\n|---|---|\n|160|3.5|\n|170|2.4|\n\n注：测试三次取均值。",encoding="utf8")
    e=extract(path,".md"); table=next(b for b in e.blocks if b.kind=="document_table")
    assert table.fields["rows"]==[["160","3.5"],["170","2.4"]]
    assert "适用纯棉" in table.text and "测试三次" in table.text
    assert len([c for c in split_blocks(e.blocks) if c["kind"]=="document_table"])==1
    from backend.document_tables import table_blocks
    long=table_blocks([["批次","观测值"],*[[f"TX{i}","x"*100] for i in range(120)]],"表1")[0]
    chunks=split_blocks([long])
    assert len(chunks)>1 and all("批次" in c["text"] for c in chunks)
    assert sum(len(c["fields"]["rows"]) for c in chunks)==120


def test_html_multi_level_headers_and_docx_order(tmp_path):
    p=tmp_path/"a.html";p.write_text('<p>上文</p><table><tr><th rowspan="2">机台</th><th colspan="2">质量</th></tr><tr><th>数量</th><th>比率</th></tr><tr><td>A</td><td>10</td><td>1%</td></tr></table><p>表下注释</p>',encoding="utf8")
    table=next(b for b in extract(p,".html").blocks if b.kind=="document_table")
    assert table.fields["headers"]==["机台","质量 / 数量","质量 / 比率"]
    assert "上文" in table.text and "表下注释" in table.text
    from docx import Document
    d=Document();d.add_paragraph("前文");t=d.add_table(rows=2,cols=2)
    for c,v in zip([t.cell(0,0),t.cell(0,1),t.cell(1,0),t.cell(1,1)],["温度","缩水率","160","3.2"]):c.text=v
    d.add_paragraph("后文");p=tmp_path/"b.docx";d.save(p)
    blocks=extract(p,".docx").blocks
    assert [b.kind for b in blocks]==["text","document_table","text"]
    assert "前文" in blocks[1].text and "后文" in blocks[1].text


def test_formula_only_rows_percent_formats_and_side_by_side_regions(tmp_path):
    w=Workbook();s=w.active
    s.append(['金额',None,'占比']);s.append(['=1+2',None,.03]);s['C2'].number_format='0.0%'
    p=tmp_path/'regions.xlsx';w.save(p)
    e=extract(p,'.xlsx')
    assert len(e.datasets)==2 and len({d['key'] for d in e.datasets})==2
    assert e.datasets[0]['row_count']==1 and e.datasets[0]['missing_formula_cache']==1
    assert e.datasets[1]['rows']==[[2,'0.03']] and '原始小数' in e.datasets[1]['columns'][0]['display_note']


def test_case_insensitive_duplicate_headers_dates_and_percent_text(storage,tmp_path):
    doc=csv_doc(storage,tmp_path,'A,a,_source_row,日期,比率\n1,2,original,2026-10-02,3%\n')
    store,_=storage;d=list_datasets(store,doc)[0]
    assert [c['name'] for c in d['columns']][:3]==['A','a_2','_source_row_2']
    result=query_datasets(store,[d['id']],'''SELECT "A", "a_2", CAST(REPLACE("比率", '%', '') AS DOUBLE)/100 AS ratio FROM data WHERE CAST("日期" AS DATE) >= DATE '2026-10-01' ''','按日期过滤，将百分比文本转换为小数')
    assert result['rows']==[[1,2,.03]]


def test_excel_blank_separated_records_are_not_silently_treated_as_headers(tmp_path):
    w=Workbook();s=w.active;s.append(['批次','数量']);s.append(['A',10]);s.append([]);s.append(['B',20]);s.append(['C',30])
    p=tmp_path/'ambiguous.xlsx';w.save(p)
    with pytest.raises(ValueError,match='区域首行'):
        extract(p,'.xlsx')


def test_excel_zero_padded_identifier_is_preserved(tmp_path):
    w=Workbook();s=w.active;s.append(['编号','数量']);s.append([12,3]);s['A2'].number_format='00000'
    p=tmp_path/'identifiers.xlsx';w.save(p)
    e=extract(p,'.xlsx')
    assert e.datasets[0]['rows']==[[2,'00012','3']]


def test_docx_merged_headers_without_explicit_header_flag(tmp_path):
    from docx import Document
    d=Document();t=d.add_table(rows=4,cols=3)
    t.cell(0,0).merge(t.cell(0,2)).text='质量检测'
    t.cell(1,0).merge(t.cell(2,0)).text='机台'
    t.cell(1,1).merge(t.cell(1,2)).text='质量'
    t.cell(2,1).text='不良数';t.cell(2,2).text='检验数'
    for i,v in enumerate(['A','1','100']):t.cell(3,i).text=v
    p=tmp_path/'merged.docx';d.save(p)
    table=extract(p,'.docx').blocks[0]
    assert table.fields['headers']==['机台','质量 / 不良数','质量 / 检验数']
    assert table.fields['rows']==[['A','1','100']] and table.fields['source_rows']==[4]
    assert '质量检测' in table.text and table.fields['structure']['spans']


def test_catalog_cannot_supply_model_organized_chart(storage,tmp_path):
    from backend.charts import chart
    doc=csv_doc(storage,tmp_path,'x,y\n1,2\n')
    with pytest.raises(ValueError,match='目录'):
        chart(storage[0],{'series':[{'type':'bar','data':[999]}]},[storage[0].chunks(doc)[0]['id']])


def test_dataset_delete_removes_assets_and_query_records(storage,tmp_path):
    from backend.jobs import JobQueue
    from backend.pipeline import IngestionRunner
    from test_ingestion_jobs import configuration
    from test_multimodal import FakeVision
    doc=csv_doc(storage,tmp_path,'x,y\n1,2\n');store,index=storage
    dataset=list_datasets(store,doc)[0]
    r=query_datasets(store,[dataset['id']],'SELECT * FROM data','完整明细')
    runner=IngestionRunner(store,index,configuration(store.root),FakeVision)
    mutation=runner.queue.reserve_mutation(doc,'deleting',1)
    runner.queue.delete_document(mutation)
    runner.cleanup_garbage()
    assert not (store.root/'datasets'/doc).exists()
    assert not store.query('SELECT * FROM datasets') and not store.query('SELECT * FROM dataset_queries')
    with pytest.raises(ValueError):
        data_chart(store,r['query_id'],'x',['y'])
