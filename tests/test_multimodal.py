import asyncio
import base64
import io
import json
import re
import sqlite3
import time
from collections import Counter
from types import SimpleNamespace

import pymupdf as fitz
import pytest
from PIL import Image

from backend.ingest import Block, extract, split_blocks
from backend.media import ANCHOR, marker, retrieval_text
from backend.multimodal import collect, enrich, persist_images
from backend.store import Store, now, uid


def png(color='navy'):
    image=Image.new('RGB',(180,120),color)
    buf=io.BytesIO();image.save(buf,format='PNG')
    return buf.getvalue()


class FakeVision:
    model='fixture-vision'
    def __init__(self,decision='retain',category='fabric',readable=False):
        self.calls=[];self.decision=decision;self.category=category;self.readable=readable
    async def analyze(self,image,context):
        self.calls.append((image,context))
        return dict(decision=self.decision,category=self.category,description='蓝灰人字纹测试图，不能据图判断成分',
                    transcription='克重: 145 g/m2' if self.readable else '',has_readable_text=self.readable,
                    reason='外观信息需结合原图' if self.decision=='retain' else '可完整转写',confidence=.95)


def register(storage,path):
    store,index=storage;doc_id=uid()
    store.execute('INSERT INTO documents(id,name,suffix,size,status,created_at,updated_at,multimodal) VALUES(?,?,?,?,?,?,?,?)',
                  (doc_id,path.name,path.suffix,path.stat().st_size,'ready',now(),now(),1))
    (store.root/'uploads'/f'{doc_id}{path.suffix}').write_bytes(path.read_bytes())
    return doc_id


async def publish(storage,path,vision):
    store,index=storage;doc_id=register(storage,path)
    result=await enrich(collect(path,path.suffix),vision)
    images=persist_images(result,store.root,doc_id,1)
    chunks=split_blocks(result.blocks)
    for c in chunks:c.update(id=uid(),revision=1)
    index.replace(doc_id,chunks,1);store.replace_chunks(doc_id,chunks,images)
    return doc_id,result


@pytest.mark.parametrize('offset',[0,200,390,419,420,421,800])
def test_anchor_counts_as_one_unit_and_has_one_chunk_owner(offset):
    image_id=uid();im={'id':image_id,'description':'蓝色织物'}
    block=Block('甲'*offset+marker(image_id)+'乙'*900,'第 1 页',images=[im])
    chunks=split_blocks([block],420,60)
    assert Counter(i for c in chunks for i in c.get('image_ids',[]))=={image_id:1}
    for c in chunks:
        logical=ANCHOR.sub('X',c['text'])
        assert len(logical)<=420
        assert '[[IMG:' not in logical and ']]' not in logical
        assert [im['id'] for im in c.get('images',[])]==c.get('image_ids',[])


def test_one_passage_can_own_multiple_images_without_global_leak():
    ids=[uid(),uid()]
    images=[{'id':i,'description':'纹理'} for i in ids]
    blocks=[Block('前文'+marker(ids[0])+'两图之间'+marker(ids[1])+'后文','段落 1',images=images),Block('另一段与图无关的说明','段落 2')]
    chunks=split_blocks(blocks)
    assert chunks[0]['image_ids']==ids
    assert not chunks[1].get('image_ids')


def test_long_standalone_description_remains_independent_with_its_original():
    image_id=uid();im={'id':image_id,'description':'蓝灰纹理'}
    block=Block('视觉描述。'*150+marker(image_id),'图片 1','visual',images=[im])
    chunks=split_blocks([block])
    assert len(chunks)==1 and chunks[0]['image_ids']==[image_id] and '视觉描述。'*150 in chunks[0]['text']


@pytest.mark.parametrize('suffix,text',[('.txt','纯文字工艺内容'),('.md','# 工艺\n\n纯文字内容'),('.csv','机台,不良率_pct\nA,1.2')])
async def test_no_images_falls_back_without_any_model_call(tmp_path,suffix,text):
    path=tmp_path/f'plain{suffix}';path.write_text(text,encoding='utf8')
    vision=FakeVision();result=await enrich(collect(path,suffix),vision)
    baseline=extract(path,suffix)
    assert not vision.calls and not result.image_assets
    assert [(b.text,b.fields) for b in result.blocks]==[(b.text,b.fields) for b in baseline.blocks]


async def test_textifiable_picture_is_converted_at_its_point(tmp_path):
    data=base64.b64encode(png()).decode()
    p=tmp_path/'inline.html';p.write_text(f'<p>前文<img src="data:image/png;base64,{data}">后文</p>',encoding='utf8')
    v=FakeVision('text','text',True);e=await enrich(collect(p,'.html'),v)
    text=e.blocks[0].text
    assert text.index('前文')<text.index('克重: 145 g/m2')<text.index('后文')
    assert not ANCHOR.search(text) and not e.blocks[0].images and len(v.calls)==1


async def test_vision_context_centres_on_picture_point_not_document_start(tmp_path):
    data=base64.b64encode(png()).decode()
    p=tmp_path/'long.html'
    p.write_text(f'<p>{"开头无关信息"*500}局部相邻标记<img src="data:image/png;base64,{data}">图片之后的说明</p>',encoding='utf8')
    vision=FakeVision();await enrich(collect(p,'.html'),vision)
    context=vision.calls[0][1]
    assert '局部相邻标记' in context and '图片之后的说明' in context and len(context)<1800
    assert '开头无关信息'*150 not in context


async def test_imageless_standalone_gets_description_and_original_even_if_model_says_text(tmp_path):
    p=tmp_path/'cloth.png';p.write_bytes(png())
    # Defensive enforcement of the standalone/no-readable-text requirement.
    e=await enrich(collect(p,'.png'),FakeVision('text','diagram',False))
    assert e.blocks[0].kind=='visual' and '蓝灰人字纹' in e.blocks[0].text
    assert len(e.blocks[0].images)==1 and ANCHOR.search(e.blocks[0].text)


async def test_docx_inline_order_and_multiple_attachments(tmp_path):
    from docx import Document
    from docx.shared import Inches
    d=Document();p=d.add_paragraph('前文')
    p.add_run().add_picture(io.BytesIO(png()),width=Inches(1))
    p.add_run('中间文字');p.add_run().add_picture(io.BytesIO(png('maroon')),width=Inches(1));p.add_run('后文')
    d.add_paragraph('完全无图的下一段')
    path=tmp_path/'inline.docx';d.save(path)
    e=await enrich(collect(path,'.docx'),FakeVision())
    assert len(e.image_assets)==2
    text=e.blocks[0].text;ids=[im['id'] for im in e.blocks[0].images]
    assert text.index('前文')<text.index(marker(ids[0]))<text.index('中间文字')<text.index(marker(ids[1]))<text.index('后文')
    assert not e.blocks[1].images


async def test_docx_header_images_are_not_mistaken_for_image_free_document(tmp_path):
    from docx import Document
    from docx.shared import Inches
    d=Document();header=d.sections[0].header
    header.paragraphs[0].add_run().add_picture(io.BytesIO(png()),width=Inches(1))
    d.add_paragraph('正文没有图片')
    path=tmp_path/'header.docx';d.save(path)
    e=await enrich(collect(path,'.docx'),FakeVision())
    assert len(e.image_assets)==1 and any(b.images and '页眉' in b.locator for b in e.blocks)
    assert not next(b for b in e.blocks if '正文没有图片' in b.text).images


async def test_docx_footer_picture_is_after_body_and_locally_attached(tmp_path):
    from docx import Document
    from docx.shared import Inches
    d=Document();d.add_paragraph('正文先出现')
    d.sections[0].footer.paragraphs[0].add_run().add_picture(io.BytesIO(png()),width=Inches(1))
    p=tmp_path/'footer.docx';d.save(p)
    e=await enrich(collect(p,'.docx'),FakeVision())
    assert e.blocks[0].text=='正文先出现' and not e.blocks[0].images
    assert '页脚' in e.blocks[-1].locator and e.blocks[-1].images


async def test_embedded_markdown_picture_has_precise_point_and_table_order(tmp_path):
    data=base64.b64encode(png()).decode()
    p=tmp_path/'cloth.md';p.write_text(f'前文 ![布样](data:image/png;base64,{data}) 后文\n|批次|克重|\n|---|---|\n|A|145|\n尾段',encoding='utf8')
    e=await enrich(collect(p,'.md'),FakeVision())
    assert e.blocks[0].images and e.blocks[1].kind=='document_table' and e.blocks[2].text=='尾段'
    text=e.blocks[0].text
    assert text.index('前文')<text.index(marker(e.blocks[0].images[0]['id']))<text.index('后文')


async def test_xlsx_images_attach_to_actual_row_and_support_multiple(tmp_path):
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    w=Workbook();s=w.active;s.append(['批次','克重']);s.append(['A',145]);s.append(['B',150])
    for color in ['navy','maroon']:s.add_image(XLImage(io.BytesIO(png(color))),'C3')
    p=tmp_path/'cloth.xlsx';w.save(p)
    e=await enrich(collect(p,'.xlsx'),FakeVision())
    rows=[b for b in e.blocks if b.kind=='visual']
    assert len(rows)==2 and all(len(r.images)==1 and '行 3' in r.locator for r in rows)
    chunks=split_blocks(rows);assert sum(len(c['image_ids']) for c in chunks)==2
    assert len(e.datasets)==1 and e.datasets[0]['row_count']==2


async def test_pptx_images_stay_on_their_slide(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches
    p=Presentation();slide=p.slides.add_slide(p.slide_layouts[5]);slide.shapes.title.text='第一组布样'
    slide.shapes.add_picture(io.BytesIO(png()), Inches(1), Inches(2),width=Inches(1))
    other=p.slides.add_slide(p.slide_layouts[5]);other.shapes.title.text='第二页没有图'
    path=tmp_path/'cloth.pptx';p.save(path)
    e=await enrich(collect(path,'.pptx'),FakeVision())
    assert any(b.images and b.locator=='幻灯片 1' for b in e.blocks)
    assert not any(b.images for b in e.blocks if b.locator=='幻灯片 2')


async def test_pdf_rasters_and_vector_art_are_located_in_display_space(tmp_path):
    path=tmp_path/'images.pdf'
    with fitz.open() as pdf:
        page=pdf.new_page(width=600,height=800)
        page.insert_text((40,60),'Before pictures')
        page.insert_image(fitz.Rect(40,100,220,220),stream=png())
        page.insert_image(fitz.Rect(260,100,440,220),stream=png('maroon'))
        page.insert_text((40,300),'After pictures')
        page.draw_rect(fitz.Rect(60,400,260,520),color=(.5,.2,.7),fill=(.8,.6,.9))
        page.set_rotation(90);pdf.save(path)
    e=await enrich(collect(path,'.pdf'),FakeVision())
    assert len(e.image_assets)>=3 and any(c.source_type=='pdf_vector' for c in e.image_assets)
    assert len(e.blocks[0].images)>=3
    assert all(c.bbox and c.preview for c in e.image_assets if c.source_type=='pdf_raster')


def test_no_external_image_fetch_and_explicit_missing_media_warning(tmp_path):
    p=tmp_path/'remote.html';p.write_text('<p>正文<img src="http://169.254.169.254/secret" alt="未提供配图"></p>',encoding='utf8')
    e=collect(p,'.html')
    assert not e.image_assets and any('未访问外部URL' in w for w in e.warnings)


async def test_invalid_model_result_is_not_silent_success(tmp_path):
    p=tmp_path/'cloth.png';p.write_bytes(png())
    class Broken:
        async def analyze(self,*args):raise ValueError('vision failed')
    with pytest.raises(ValueError):await enrich(collect(p,'.png'),Broken())


def test_image_limit_fails_instead_of_dropping_remainder(tmp_path):
    data=base64.b64encode(png()).decode()
    p=tmp_path/'many.html';p.write_text(f'<p><img src="data:image/png;base64,{data}"><img src="data:image/png;base64,{data}"></p>')
    with pytest.raises(ValueError,match='超过1张'):collect(p,'.html',limit=1)


async def test_store_and_retrieval_return_only_passage_local_images(storage,tmp_path):
    p=tmp_path/'cloth.png';p.write_bytes(png())
    doc_id,_=await publish(storage,p,FakeVision());s,index=storage
    rows=s.chunks(doc_id);assert len(rows[0]['images'])==1
    image=rows[0]['images'][0]
    assert image['url']==f"/api/images/{image['id']}" and (s.root/'assets'/doc_id/f"{image['id']}.png").exists()
    hits=index.search(s,'蓝灰人字纹',[doc_id])
    assert hits and hits[0]['images'][0]['id']==image['id']
    assert '[[IMG:' not in retrieval_text(hits[0]) and '蓝灰人字纹' in retrieval_text(hits[0])


def test_legacy_store_migrates_additively(tmp_path):
    root=tmp_path/'legacy';root.mkdir();path=root/'metadata.sqlite3'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE documents(id TEXT PRIMARY KEY,name TEXT,suffix TEXT,size INTEGER,status TEXT,progress INTEGER DEFAULT 0,error TEXT DEFAULT "",warnings TEXT DEFAULT "[]",created_at TEXT,updated_at TEXT,revision INTEGER DEFAULT 1,chunks INTEGER DEFAULT 0,pages INTEGER DEFAULT 0,ocr_pages INTEGER DEFAULT 0)')
        c.execute('INSERT INTO documents(id,name,status) VALUES("legacy","旧文档","ready")')
    s=Store(root)
    d=s.document('legacy');assert d['name']=='旧文档' and d['multimodal'] is False and d['image_count']==0


async def test_agent_carries_source_images_and_sends_pixels_in_user_message(storage,tmp_path):
    from backend.agent import TextileAgent
    from test_agent import Message
    p=tmp_path/'cloth.png';p.write_bytes(png())
    doc_id,_=await publish(storage,p,FakeVision());s,index=storage
    row=s.chunks(doc_id)[0];requests=[]
    call={'id':'call1','type':'function','function':{'name':'search_knowledge','arguments':json.dumps({'query':'蓝灰人字纹','document_id':doc_id})}}
    responses=[Message(calls=[call]),Message(f"可见蓝灰纹理。[C:{row['id']}]")]
    async def create(**kwargs):
        requests.append(kwargs)
        return responses.pop(0).stream()
    a=TextileAgent(s,index);await a.client.close();a.client=SimpleNamespace(responses=SimpleNamespace(create=create))
    async def emit(*args):pass
    result=await a.run('fixture','看布料',emit,[doc_id])
    assert result['sources'][0]['images'][0]['id']==row['images'][0]['id']
    pixels=[m for m in requests[1]['input'] if isinstance(m.get('content'),list) and m.get('role')=='user']
    assert pixels and any(p['type']=='input_image' and p['image_url'].startswith('data:image/jpeg;base64,') for p in pixels[-1]['content'])
    assert 'base64' not in json.dumps(result)
