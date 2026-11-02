"""Real DeepSeek/BGE acceptance for charts extracted from ordinary prose.

Creates isolated synthetic fixtures in .cache; never changes the running library.
Calls the configured Responses model and renders its options offline with ECharts.
"""
import asyncio
import json
import math
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.agent import TextileAgent
from backend.config import settings
from backend.ingest import extract,split_blocks
from backend.retrieval import VectorIndex
from backend.store import Store,now,uid


def numbers(value):
    if isinstance(value,bool):return []
    if isinstance(value,(float,int)):return [value]
    if isinstance(value,list):return [n for item in value for n in numbers(item)]
    if isinstance(value,dict):return [n for item in value.values() for n in numbers(item)]
    return []


def plotted_numbers(artifact):
    option=artifact.get('option',{})
    series=option.get('series',[]);series=[series] if isinstance(series,dict) else series
    datasets=option.get('dataset',[]);datasets=[datasets] if isinstance(datasets,dict) else datasets
    return numbers([s.get('data',[]) for s in series]+[d.get('source',[]) for d in datasets])


async def main():
    report={'synthetic':True,'checks':{},'browser_acceptance':False,'http_acceptance':False}
    report_path=ROOT/'artifacts'/'acceptance-charts.json'
    work=(ROOT/'.cache'/('chart-acceptance-'+uuid4().hex[:10])).resolve()
    assert work.parent==(ROOT/'.cache').resolve()
    work.mkdir(parents=True)
    def check(name,value):
        report['checks'][name]=bool(value)
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print(name,'PASS' if value else 'FAIL',flush=True)
        assert value,name
    settings.embedding_cache_dir=settings.embedding_cache_dir or ROOT/'data'/'models'
    store=Store(work);index=VectorIndex(work,settings.embedding_model,settings.onnx_provider)
    agent=TextileAgent(store,index)
    try:
        source=work/'article.txt'
        source.write_text('模拟纺织研发文章，非真实生产记录。配方中的纤维质量占比为棉45%、涤纶35%、粘胶20%。同一模拟布样试验中，定型温度150℃时经向缩水率为5.1%，165℃时为3.2%，180℃时为2.45%。这些数值为三个独立观测，不代表因果或最优工况。本文没有表格。',encoding='utf8')
        doc_id=uid()
        store.execute('INSERT INTO documents(id,name,suffix,size,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(doc_id,source.name,'.txt',source.stat().st_size,'ready',now(),now()))
        chunks=split_blocks(extract(source,'.txt').blocks)
        for row in chunks:row.update(id=uid(),revision=1)
        await asyncio.to_thread(index.replace,doc_id,chunks,1)
        store.replace_chunks(doc_id,chunks);store.update_doc(doc_id,chunks=len(chunks))
        check('prose_without_table_fields',all(not r['fields'] and r['kind']=='text' for r in chunks))
        async def emit(event,value):
            if event=='tool_start':print('tool',value['name'],flush=True)
        result=await asyncio.wait_for(agent.run('chart-acceptance','请先查阅这篇模拟文章。把纤维占比画成没有X/Y坐标轴的环形图，把三组温度与缩水率观测画为数值散点图；只用原文明示数据，写清单位和引用，不需要任何表格列名。生成两张图后简述。',emit,[doc_id]),240)
        charts=[a for a in result['artifacts'] if a['type']=='chart']
        check('two_charts_from_prose',len(charts)>=2)
        def has_type(a,kind):
            series=a['option'].get('series',[]);series=[series] if isinstance(series,dict) else series
            return any(s.get('type')==kind for s in series)
        pie=next(a for a in charts if has_type(a,'pie'))
        scatter=next(a for a in charts if has_type(a,'scatter'))
        check('axis_free_donut','xAxis' not in pie['option'] and 'yAxis' not in pie['option'])
        check('composition_values_preserved',all(any(math.isclose(v,n) for n in plotted_numbers(pie)) for v in [45,35,20]))
        check('observations_preserved',all(any(math.isclose(v,n) for n in plotted_numbers(scatter)) for v in [150,5.1,165,3.2,180,2.45]))
        check('sources_from_article',all(set(a['source_ids'])<={r['id'] for r in chunks} for a in charts))
        check('tools_completed',all(t['ok'] for t in result['trace']))
        config=work/'charts.json';config.write_text(json.dumps(charts,ensure_ascii=False),encoding='utf8')
        rendered=await asyncio.to_thread(subprocess.run,['node',str(ROOT/'scripts'/'render_chart_check.mjs'),str(config)],cwd=ROOT,capture_output=True,text=True,check=True)
        check('generated_options_render',json.loads(rendered.stdout)['rendered']==len(charts))
        report.update(passed=True,chart_count=len(charts),model=result['model'])
    except BaseException as exc:
        report.update(passed=False,error_type=type(exc).__name__)
        raise
    finally:
        await agent.client.close()
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
