"""Live API acceptance against the running services. Uses REAL BGE/OCR/DeepSeek.

Idempotent fixture import by name; writes factual machine-readable evidence.
Browser acceptance is recorded separately, never inferred from API success.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT=Path(__file__).resolve().parents[1]
REPORT=ROOT/'artifacts'/'acceptance-api.json'


def main():
    report={'started_at':datetime.now(timezone.utc).isoformat(),'checks':[], 'browser_acceptance': 'not covered by this script'}
    def check(name,condition,details=None):
        report['checks'].append({'name':name,'passed':bool(condition),'details':details})
        REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print(name, 'PASS' if condition else 'FAIL',flush=True)
        if not condition:raise AssertionError(name)
    with httpx.Client(base_url='http://127.0.0.1:8000',timeout=360,trust_env=False) as c:
        h=c.get('/api/health');h.raise_for_status();check('backend_health_and_key',h.json()['api_key_configured'],h.json())
        docs=c.get('/api/documents').json()
        by_name={d['name']:d for d in docs}
        targets=[p for p in sorted((ROOT/'samples').iterdir()) if p.suffix in {'.csv','.md','.txt','.pdf','.png'} and p.name!='README.md']
        ids={}
        for p in targets:
            if p.name in by_name:
                ids[p.name]=by_name[p.name]['id'];continue
            with p.open('rb') as f:r=c.post('/api/documents',files={'file':(p.name,f)})
            r.raise_for_status();ids[p.name]=r.json()['id']
        deadline=time.monotonic()+240
        while time.monotonic()<deadline:
            docs=c.get('/api/documents').json();selected=[d for d in docs if d['id'] in ids.values()]
            if all(d['status'] in {'ready','error'} for d in selected):break
            time.sleep(1)
        check('real_sample_import',len(selected)==len(targets) and all(d['status']=='ready' for d in selected),selected)
        scan=next(d for d in selected if d['name'].startswith('06_'))
        scan_chunks=c.get(f"/api/documents/{scan['id']}/chunks").json()
        scan_text='\n'.join(x['text'] for x in scan_chunks)
        check('raster_pdf_real_chinese_ocr',scan['ocr_pages']==1 and '146' in scan_text and '4.2' in scan_text and 'TX-202610-003' in scan_text,scan_chunks)
        mixed=next(d for d in selected if d['name'].startswith('07_'))
        check('mixed_pdf_text_and_image_ocr',mixed['ocr_pages']>0 and mixed['chunks']>=2,mixed)
        def ask(question):
            chat=c.post('/api/chats').json()['id'];events=[];result=None
            with c.stream('POST',f'/api/chats/{chat}/stream',json={'content':question}) as r:
                r.raise_for_status();name=''
                for line in r.iter_lines():
                    if line.startswith('event:'):name=line[6:].strip()
                    elif line.startswith('data:'):
                        data=json.loads(line[5:]);events.append({'event':name,'data':data})
                        if name=='error':raise RuntimeError(data['message'])
                        if name=='result':result=data
            check('chat_persisted_'+chat,len(c.get(f'/api/chats/{chat}/messages').json())==2)
            return chat,result,events
        question='请检索已导入的模拟数据：1.按机台对比MES生产日报的平均不良率，生成柱状图；2.根据定型工况实验，生成定型温度与平均缩水率趋势折线图，并创建可拖动温度滑块查看历史相近工况建议。请引用数据来源，说明平均口径和历史工况限制。'
        chat,result,events=ask(question)
        report['artifact_chat_id']=chat;report['artifact_chat_events']=events
        artifacts=result.get('artifacts',[]) if result else []
        check('real_deepseek_langgraph_tools',result is not None and any(t['name']=='list_documents' for t in result['trace']) and all(t['ok'] for t in result['trace']),result.get('trace') if result else None)
        charts=[a for a in artifacts if a['type']=='chart'];controls=[a for a in artifacts if a['type']=='scenario']
        check('chart_and_interactive_control',len(charts)>=2 and len(controls)>=1,artifacts)
        mes=next((a for a in charts if a['metric']=='不良率_pct'),None)
        check('chart_exact_ground_truth',mes and {p['name']:p['value'] for p in mes['data']}=={'定型A':1.25,'定型B':2.1,'定型C':.85},mes)
        scenario=controls[0]
        body={k:scenario[k] for k in ['document_id','parameter','metric','direction','revision']}
        low=c.post('/api/scenarios/evaluate',json={**body,'value':150});high=c.post('/api/scenarios/evaluate',json={**body,'value':180})
        check('slider_backend_values_change',low.status_code==200 and high.status_code==200 and low.json()['reference']==5.1 and high.json()['reference']==2.45,{'150':low.json(),'180':high.json()})
        _,ocr_answer,ocr_events=ask('请只根据纯扫描质检报告，给出批次TX-202610-003的克重、经向缩水率和结论，必须引用原文分块，并说明OCR人工校对要求。')
        check('ocr_rag_answer_with_evidence',ocr_answer and '146' in ocr_answer['content'] and '4.2' in ocr_answer['content'] and any(s['document_id']==scan['id'] for s in ocr_answer['sources']) and '[C:' in ocr_answer['content'],ocr_answer)
        report['ocr_chat_events']=ocr_events
        _,unknown,_=ask('请查找批号 ZZ-NOT-EXIST-999 的实际实测克重和供应商价格。没有该批号的证据就明确说未找到，不要用其他批次代替。')
        check('missing_evidence_not_substituted',unknown and any(t in unknown['content'] for t in ['未找到','没有','未检索','不存在','未发现']),unknown)
        # Edit and deletion use a disposable document, preserving the seeded ground truth.
        r=c.post('/api/documents',files={'file':('acceptance_disposable.txt','临时验收纺织文档。原始克重为100。'.encode())});r.raise_for_status();did=r.json()['id']
        for _ in range(60):
            d=next(x for x in c.get('/api/documents').json() if x['id']==did)
            if d['status'] in {'ready','error'}:break
            time.sleep(.3)
        chunk=c.get(f'/api/documents/{did}/chunks').json()[0]
        r=c.put('/api/chunks/'+chunk['id'],json={'text':'临时验收纺织文档。人工校对后克重为123。','revision':chunk['revision']})
        check('real_embedding_edit_revision',r.status_code==200 and r.json()['revision']==2 and '123' in r.json()['text'],r.json())
        check('remove_original_chunks_vectors',c.delete('/api/documents/'+did).status_code==200 and c.get(f'/api/documents/{did}/chunks').status_code==404)
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        report['status']='passed'
        REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print('Live API acceptance passed. Evidence:',REPORT,flush=True)


if __name__=='__main__':main()
