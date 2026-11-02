"""Real DeepSeek Responses + vision + local BGE/LanceDB acceptance.

Uses only marked synthetic samples in an isolated .cache directory; does not
write to the running knowledge base or overwrite sample files. Calls are billed.
This SDK/Agent test does not cover browser rendering or HTTP image delivery.
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from backend.agent import TextileAgent
from backend.config import settings
from backend.ingest import extract,split_blocks
from backend.multimodal import DeepSeekVision,collect,enrich,persist_images
from backend.retrieval import VectorIndex
from backend.store import Store,now,uid


async def main():
    report={"api":"responses","synthetic":True,"browser_acceptance":False,"http_acceptance":False,"checks":{}}
    report_path=ROOT/'artifacts'/'acceptance-responses.json'
    work=(ROOT/'.cache'/('responses-acceptance-'+uuid4().hex[:10])).resolve()
    assert work.parent==(ROOT/'.cache').resolve()
    work.mkdir(parents=True)
    settings.embedding_cache_dir=settings.embedding_cache_dir or ROOT/'data'/'models'
    store=Store(work)
    index=VectorIndex(work,settings.embedding_model,settings.onnx_provider)
    agent=TextileAgent(store,index)
    started=time.monotonic()
    def check(name,value):
        report['checks'][name]=bool(value)
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print(name,'PASS' if value else 'FAIL',flush=True)
        assert value,name
    async def publish(path,multimodal=False):
        doc_id=uid()
        store.execute('INSERT INTO documents(id,name,suffix,size,status,created_at,updated_at,multimodal) VALUES(?,?,?,?,?,?,?,?)',
                      (doc_id,path.name,path.suffix,path.stat().st_size,'parsing',now(),now(),multimodal))
        (work/'uploads'/(doc_id+path.suffix)).write_bytes(path.read_bytes())
        result=await asyncio.to_thread(collect if multimodal else extract,path,path.suffix)
        if multimodal:
            await enrich(result,DeepSeekVision(agent.client.with_options(max_retries=0),settings.deepseek_vision_model or settings.deepseek_model))
        assets=persist_images(result,work,doc_id,1)
        chunks=split_blocks(result.blocks)
        for chunk in chunks:chunk.update(id=uid(),revision=1)
        await asyncio.to_thread(index.replace,doc_id,chunks,1)
        store.replace_chunks(doc_id,chunks,assets)
        store.update_doc(doc_id,status='ready',chunks=len(chunks),image_count=len(assets),retained_images=sum(im['decision']=='retain' for im in assets))
        return doc_id
    try:
        check('key_configured',bool(settings.deepseek_api_key))
        table=await publish(ROOT/'samples'/'02_定型工况实验_模拟.csv')
        image=await publish(ROOT/'samples'/'multimodal'/'01_blue_herringbone_synthetic.png',True)
        check('responses_vision_import',store.document(image)['retained_images']==1)
        chat_id=uid()
        store.execute('INSERT INTO chats VALUES(?,?,?)',(chat_id,'Responses真实验收',now()))
        question='请分步处理：先用一句简短回复告诉我将核对定型工况和布样，随即调用工具，不等我回复。检索布样并查看附图，描述可见颜色和织纹；根据CSV完整数据生成定型温度_C与缩水率_pct折线图，以及温度滑块。可以在工具步骤之间回复阶段性结论，再继续调用工具。最后简述结果并引用来源。所有资料均为模拟，不要作为真实生产配方。'
        store.add_message(chat_id,'user',{'content':question})
        events=[]
        async def emit(event,value):
            events.append((event,value))
            if event in {'assistant_message','tool_start','artifact'}:
                print('event',event,value.get('name') or value.get('phase') or value.get('artifact',{}).get('type'),flush=True)
        result=await asyncio.wait_for(agent.run(chat_id,question,emit,[table,image]),300)
        store.add_message(chat_id,'assistant',result)
        check('responses_completed',result['status']=='completed' and result['api']=='responses')
        text_positions=[i for i,(name,_) in enumerate(events) if name=='assistant_delta']
        tool_positions=[i for i,(name,_) in enumerate(events) if name=='tool_start']
        check('reply_then_tools',bool(text_positions and tool_positions and min(text_positions)<min(tool_positions)))
        check('reply_after_tools',bool(text_positions and max(text_positions)>max(tool_positions)))
        check('chart_and_control',{a['type'] for a in result['artifacts']}=={'chart','scenario'})
        check('retrieval_returns_images',any(s.get('images') for s in result['sources']))
        check('model_received_retrieved_image',bool(result['vision_images_used']))
        check('history_preserves_timeline',store.messages(chat_id)[-1]['parts']==result['parts'])
        check('history_contains_no_image_payload_or_reasoning','base64' not in json.dumps(result) and not any(p['type']=='reasoning' for p in result['parts']))
        report.update(passed=True,elapsed_seconds=round(time.monotonic()-started,2),
                      tool_names=[t['name'] for t in result['trace']],part_types=[p['type'] for p in result['parts']],model=result['model'])
    except BaseException as exc:
        report.update(passed=False,error_type=type(exc).__name__)
        raise
    finally:
        await agent.client.close()
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')


if __name__=='__main__':
    asyncio.run(main())
