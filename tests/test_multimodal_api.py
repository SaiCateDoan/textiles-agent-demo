import time
import io
import pytest

from test_api import client
from test_multimodal import FakeVision, png


def wait_doc(client,doc_id):
    for _ in range(200):
        doc=next(d for d in client.get('/api/documents').json() if d['id']==doc_id)
        if doc['status'] in {'ready','error'}:return doc
        time.sleep(.03)
    raise AssertionError('Background ingestion did not finish')


def test_upload_defaults_off_and_text_fallback_never_requires_api_key(client,monkeypatch):
    from backend import main
    monkeypatch.setattr(main.settings,'deepseek_api_key','')
    def forbidden(*args):raise AssertionError('Vision constructor must not run for image-free files')
    monkeypatch.setattr(main,'DeepSeekVision',forbidden)
    for form in [{},{'multimodal':'true'}]:
        r=client.post('/api/documents',data=form,files={'file':('plain.txt','纯文字工艺说明'.encode())})
        assert r.status_code==202
        d=wait_doc(client,r.json()['id'])
        assert d['status']=='ready' and d['multimodal']==bool(form) and d['image_count']==0


def test_upload_attachment_edit_guard_reprocess_history_and_delete(client,storage,monkeypatch):
    from backend import main
    monkeypatch.setattr(main.settings,'deepseek_api_key','test-only')
    monkeypatch.setattr(main,'DeepSeekVision',lambda *args:FakeVision())
    r=client.post('/api/documents',data={'multimodal':'true'},files={'file':('cloth.png',png(),'image/png')})
    doc_id=r.json()['id'];d=wait_doc(client,doc_id)
    assert d['status']=='ready' and d['image_count']==d['retained_images']==1
    row=client.get(f'/api/documents/{doc_id}/chunks').json()[0]
    assert row['kind']=='visual' and len(row['images'])==1
    old_url=row['images'][0]['url']
    im=client.get(old_url);assert im.status_code==200 and im.headers['content-type']=='image/png' and im.content.startswith(b'\x89PNG')
    assert client.get('/api/images/not-an-id').status_code==404
    bad=client.put('/api/chunks/'+row['id'],json={'text':'误删图片标记','revision':1})
    assert bad.status_code==422
    edited=client.put('/api/chunks/'+row['id'],json={'text':'人工校对说明。'+row['text'],'revision':1})
    assert edited.status_code==200 and len(edited.json()['images'])==1
    # Preserve old media for history while a new import revision gets new IDs.
    r=client.post(f'/api/documents/{doc_id}/reprocess',json={'expected_revision':2,'confirm_replace':True})
    assert r.status_code==202 and wait_doc(client,doc_id)['status']=='ready'
    current=client.get(f'/api/documents/{doc_id}/chunks').json()[0]
    assert current['images'][0]['url']!=old_url and client.get(old_url).status_code==200
    assert client.delete(f'/api/documents/{doc_id}').status_code==200
    assert client.get(old_url).status_code==404 and client.get(current['images'][0]['url']).status_code==404
    assert not (storage[0].root/'assets'/doc_id).exists()


def test_vision_failure_is_visible_and_retry_retains_opt_in(client,storage,monkeypatch):
    from backend import main
    monkeypatch.setattr(main.settings,'deepseek_api_key','test-only')
    class Broken:
        async def analyze(self,*args):raise ValueError('视觉分析失败，测试故障')
    monkeypatch.setattr(main,'DeepSeekVision',lambda *args:Broken())
    r=client.post('/api/documents',data={'multimodal':'true'},files={'file':('cloth.png',png(),'image/png')})
    doc_id=r.json()['id'];d=wait_doc(client,doc_id)
    assert d['status']=='error' and '视觉分析失败' in d['error'] and d['multimodal'] is True
    assert not client.get(f'/api/documents/{doc_id}/chunks').json() and storage[1].table.count_rows()==0
    monkeypatch.setattr(main,'DeepSeekVision',lambda *args:FakeVision())
    assert client.post(f'/api/documents/{doc_id}/retry').status_code==202
    assert wait_doc(client,doc_id)['status']=='ready'
    assert client.get(f'/api/documents/{doc_id}/chunks').json()[0]['images']


def test_editing_table_row_keeps_anchored_image_and_numeric_fields(client,monkeypatch):
    from backend import main
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    monkeypatch.setattr(main.settings,'deepseek_api_key','test-only')
    monkeypatch.setattr(main,'DeepSeekVision',lambda *args:FakeVision())
    w=Workbook();sheet=w.active;sheet.append(['批次','克重']);sheet.append(['A',145])
    sheet.add_image(XLImage(io.BytesIO(png())),'C2')
    buf=io.BytesIO();w.save(buf)
    r=client.post('/api/documents',data={'multimodal':'true'},files={'file':('rows.xlsx',buf.getvalue())})
    doc_id=r.json()['id'];assert wait_doc(client,doc_id)['status']=='ready'
    row=client.get(f'/api/documents/{doc_id}/chunks').json()[0]
    text=row['text'].replace('克重: 145','克重: 146')
    r=client.put('/api/chunks/'+row['id'],json={'text':text,'revision':1})
    assert r.status_code==200 and r.json()['fields']['克重']=='146' and len(r.json()['images'])==1
