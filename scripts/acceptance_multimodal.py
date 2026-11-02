"""Real DeepSeek visual-ingestion and image-carrying RAG acceptance.

Creates clearly synthetic source fixtures, imports via HTTP with opt-in, and saves
asserted evidence. No test doubles. Re-running creates new demo docs/chats and
incurs vision/chat API charges. Existing knowledge is never replaced or deleted.
"""
import hashlib
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from PIL import Image, ImageChops, ImageDraw, ImageFont
from docx import Document
from docx.shared import Inches

ROOT=Path(__file__).resolve().parents[1]
SAMPLES=ROOT/'samples'/'multimodal'
REPORT=ROOT/'artifacts'/'acceptance-multimodal.json'


def make_fixtures():
    SAMPLES.mkdir(exist_ok=True)
    # Procedural illustrations, expressly NOT real textile photographs.
    blue=Image.new('RGB',(900,600),'#c7d0d0');d=ImageDraw.Draw(blue)
    for y in range(-400,1000,6):
        for x in range(0,900,60):
            offset=(x//60)%2
            d.line((x,y+offset*60,x+60,y+(1-offset)*60),fill='#536b7d',width=3)
    for x in range(0,900,3):d.line((x,0,x,600),fill='#919da044',width=1)
    blue.save(SAMPLES/'01_blue_herringbone_synthetic.png')
    orange=Image.new('RGB',(900,600),'#dfb982');d=ImageDraw.Draw(orange)
    for x in range(0,900,90):d.rectangle((x,0,x+28,600),fill='#864e37')
    for y in range(0,600,90):d.rectangle((0,y,900,y+28),fill='#b76d40')
    for x in range(0,900,5):d.line((x,0,x,600),fill='#b18a5e',width=1)
    orange.save(SAMPLES/'02_orange_check_synthetic.png')
    report=Image.new('RGB',(1300,650),'white');d=ImageDraw.Draw(report)
    font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',40)
    for i,line in enumerate(['多模态入库演示报告  模拟数据','批次: MM-TEXT-001','演示克重: 145 g/m2','演示门幅: 150 cm','结论: 仅供软件测试，不作为实际质检凭证']):
        d.text((65,65+i*103),line,font=font,fill='#1d3227')
    report.save(SAMPLES/'03_readable_report_synthetic.png')
    doc=Document();doc.add_heading('多模态纺织入库测试材料',0)
    doc.add_paragraph('本文件仅用于软件验收。所有图像由程序绘制，不是真实织物照片或检测数据。')
    p=doc.add_paragraph('素材 MM-DEMO-A/B：下面两张分别为蓝灰人字纹和橙褐格纹示意。请保留原图用于外观比较，不能推断真实成分、克重或手感。')
    p.add_run().add_picture(str(SAMPLES/'01_blue_herringbone_synthetic.png'),width=Inches(2.1))
    p.add_run('第二张示意：')
    p.add_run().add_picture(str(SAMPLES/'02_orange_check_synthetic.png'),width=Inches(2.1))
    p.add_run('两张图片属于同一段素材比较。')
    p=doc.add_paragraph('下面是可转写的模拟报告截图。')
    p.add_run().add_picture(str(SAMPLES/'03_readable_report_synthetic.png'),width=Inches(5))
    doc.add_paragraph('独立无图段落：本段只说明试样标记规则，不应被附上前面任何一张图片。')
    path=SAMPLES/'04_multimodal_demo_synthetic.docx';doc.save(path)
    plain=SAMPLES/'05_plain_fallback.txt';plain.write_text('无图回退验收：纯文本纺织说明，开启多模态也不应调用视觉模型。',encoding='utf8')
    return path,SAMPLES/'01_blue_herringbone_synthetic.png',plain


def main():
    sys.stdout.reconfigure(encoding='utf8')
    docx,standalone,plain=make_fixtures()
    result={'started_at':datetime.now(timezone.utc).isoformat(),'checks':[],'browser_acceptance':'not covered; API and actual providers only'}
    def check(name,passed,details=None):
        result['checks'].append({'name':name,'passed':bool(passed),'details':details})
        REPORT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        print(name,'PASS' if passed else 'FAIL',flush=True)
        if not passed:raise AssertionError(name)
    with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=360) as c:
        for attempt in range(30):
            try:
                c.get('/api/health').raise_for_status()
                break
            except httpx.HTTPError:
                if attempt==29:raise
                time.sleep(1)
        def upload(path):
            with path.open('rb') as f:r=c.post('/api/documents',data={'multimodal':'true'},files={'file':(path.name,f)})
            r.raise_for_status();doc_id=r.json()['id'];state=None
            for _ in range(300):
                doc=next(d for d in c.get('/api/documents').json() if d['id']==doc_id)
                if doc['status']!=state:print(path.name,doc['status'],flush=True);state=doc['status']
                if doc['status'] in {'ready','error'}:break
                time.sleep(1)
            check('import_'+path.name,doc['status']=='ready',doc)
            return doc,c.get(f'/api/documents/{doc_id}/chunks').json()
        document,chunks=upload(docx)
        result['document_id']=document['id']
        rich=[r for r in chunks if len(r.get('images',[]))>=2]
        check('multiple_images_owned_by_exact_passage',len(rich)==1 and 'MM-DEMO-A/B' in rich[0]['text'],rich)
        plain_chunk=next(r for r in chunks if '独立无图段落' in r['text'])
        check('images_not_attached_to_entire_document',not plain_chunk['images'],plain_chunk)
        check('readable_image_converted_to_text',any('MM-TEXT-001' in r['text'] and '145' in r['text'] for r in chunks),[r for r in chunks if 'MM-TEXT-001' in r['text']])
        originals=[SAMPLES/'01_blue_herringbone_synthetic.png',SAMPLES/'02_orange_check_synthetic.png']
        for im,original in zip(rich[0]['images'],originals):
            r=c.get(im['url']);r.raise_for_status()
            returned=Image.open(io.BytesIO(r.content)).convert('RGB')
            expected=Image.open(original).convert('RGB')
            equal=returned.size==expected.size and ImageChops.difference(returned,expected).getbbox() is None
            check('original_pixels_'+im['id'],r.headers['content-type']=='image/png' and equal,{'url':im['url'],'sha256':hashlib.sha256(r.content).hexdigest(),'pixels_identical_to_fixture':equal})
        image_doc,image_chunks=upload(standalone)
        check('image_without_ocr_is_independent_described_passage',len(image_chunks)>=1 and image_chunks[0]['kind']=='visual' and image_chunks[0]['images'] and '模型生成' in image_chunks[0]['text'],image_chunks)
        fallback,fallback_chunks=upload(plain)
        check('enabled_but_image_free_fallback',fallback['multimodal'] and fallback['image_count']==0 and fallback['retained_images']==0 and all(not r['images'] for r in fallback_chunks),fallback)
        chat=c.post('/api/chats').json()['id'];answer=None;events=[]
        with c.stream('POST',f'/api/chats/{chat}/stream',json={'content':'请检索素材MM-DEMO-A/B所属文段，比较该文段两张织纹示意图的可见颜色和图案差异，展示原图并引用该文段。不要推断实际成分、克重或手感。','document_ids':[document['id']]}) as response:
            response.raise_for_status();event=''
            for line in response.iter_lines():
                if line.startswith('event:'):event=line[6:].strip()
                elif line.startswith('data:'):
                    data=json.loads(line[5:]);events.append({'event':event,'data':data})
                    if event=='tool_result':print('RAG tool:',data['name'],data['ok'],flush=True)
                    if event=='error':raise RuntimeError(data['message'])
                    if event=='result':answer=data
        check('retrieval_returns_passage_with_originals',answer and any(len(s.get('images',[]))>=2 for s in answer['sources']),answer['sources'] if answer else None)
        check('rag_model_received_pixels_and_cited_passage',answer and len(answer.get('vision_images_used',[]))>=2 and f"[C:{rich[0]['id']}]" in answer['content'],{'vision_images_used':answer.get('vision_images_used'), 'content':answer.get('content')} if answer else None)
        check('persistent_chat_keeps_image_evidence',any(m['role']=='assistant' and any(s.get('images') for s in m.get('sources',[])) for m in c.get(f'/api/chats/{chat}/messages').json()))
        result.update(status='passed',chat_id=chat,events=events,finished_at=datetime.now(timezone.utc).isoformat())
        REPORT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        print('Real multimodal acceptance passed:',REPORT,flush=True)


if __name__=='__main__':main()
