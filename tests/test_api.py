import json
import pytest
from fastapi.testclient import TestClient
from conftest import load_doc


@pytest.fixture
def client(storage,monkeypatch):
    from backend import main
    monkeypatch.setattr(main,'store',storage[0]);monkeypatch.setattr(main,'index',storage[1])
    with TestClient(main.app) as c:yield c


def test_upload_real_format_status_and_delete(client,storage):
    r=client.post('/api/documents',files={'file':('质检.txt','纺织批次TX001 待复检'.encode(),'text/plain')})
    assert r.status_code==202
    doc=r.json()['id']
    # Wait for the asynchronous background worker, not just the upload receipt.
    import time
    for _ in range(100):
        d=next(x for x in client.get('/api/documents').json() if x['id']==doc)
        if d['status'] in {'ready','error'}:break
        time.sleep(.05)
    assert d['status']=='ready' and d['chunks']>0
    assert '纺织' in client.get(f'/api/documents/{doc}/chunks').json()[0]['text']
    assert client.delete(f'/api/documents/{doc}').status_code==200
    assert not storage[0].chunks(doc) and storage[1].table.count_rows()==0
    assert client.get(f'/api/documents/{doc}/file').status_code==404


def test_upload_empty_unsupported_and_traversal(client):
    assert client.post('/api/documents',files={'file':('bad.exe',b'123')}).status_code==415
    assert client.post('/api/documents',files={'file':('empty.txt',b'')}).status_code==400
    assert client.get('/api/documents/not-a-valid-id/chunks').status_code==404


def test_edit_reindexes_text_and_structured_values(client,storage,tmp_path):
    path=tmp_path/'table.md';path.write_text('| 机台 | 不良率_pct |\n|---|---|\n|A|1.2|',encoding='utf8')
    doc=load_doc(storage,path);s,idx=storage
    row=s.chunks(doc)[0]
    text=row['text'].replace('1.2','3.2')
    r=client.put('/api/chunks/'+row['id'],json={'text':text,'revision':1})
    assert r.status_code==200 and r.json()['fields']['rows']==[['A','3.2']]
    assert r.json()['revision']==2 and s.document(doc)['revision']==2
    assert {v['revision'] for v in idx.table.to_arrow().to_pylist()}=={2}
    assert client.put('/api/chunks/'+row['id'],json={'text':text,'revision':1}).status_code==409
    assert client.put('/api/chunks/'+row['id'],json={'text':'乱改表格','revision':2}).status_code==422
    assert client.put('/api/chunks/'+row['id'],json={'text':'  ','revision':2}).status_code==422


def test_scenario_stale_and_out_of_range(client,storage,sample_root):
    doc=load_doc(storage,sample_root/'02_定型工况实验_模拟.csv')
    body=dict(document_id=doc,parameter='定型温度_C',metric='缩水率_pct',value=150,revision=1)
    assert client.post('/api/scenarios/evaluate',json=body).json()['reference']==5.1
    assert client.post('/api/scenarios/evaluate',json={**body,'revision':99}).status_code==409
    assert client.post('/api/scenarios/evaluate',json={**body,'value':300}).status_code==422


def test_health_contains_no_secret(client):
    r=client.get('/api/health');assert r.status_code==200
    assert 'api_key_configured' in r.json() and 'deepseek_api_key' not in r.json()


def test_sse_result_and_persistent_history(client,storage,monkeypatch):
    from backend import main
    class FakeAgent:
        client=main.agent.client
        async def run(self,chat_id,question,emit,document_ids):
            await emit('tool_start',{'name':'list_documents','arguments':{}})
            return {'content':'测试回答','artifacts':[],'sources':[],'trace':[]}
    monkeypatch.setattr(main,'agent',FakeAgent())
    monkeypatch.setattr(main.settings,'deepseek_api_key','test-only')
    chat=client.post('/api/chats').json()['id']
    with client.stream('POST',f'/api/chats/{chat}/stream',json={'content':'测试工艺'}) as r:
        text=''.join(r.iter_text())
    assert 'event: result' in text and 'event: done' in text and '测试回答' in text
    assert len(client.get(f'/api/chats/{chat}/messages').json())==2


def test_chat_missing_key_and_invalid_input(client,monkeypatch):
    from backend import main
    monkeypatch.setattr(main.settings,'deepseek_api_key','')
    chat=client.post('/api/chats').json()['id']
    assert client.post(f'/api/chats/{chat}/stream',json={'content':'问题'}).status_code==503


def test_reprocess_version_guard_and_inline_original(client,storage,sample_root):
    import time
    doc=load_doc(storage,sample_root/'08_坯布参数_模拟.pdf')
    r=client.get(f'/api/documents/{doc}/file?inline=true')
    assert r.status_code==200 and r.headers['content-disposition'].startswith('inline;')
    assert client.post(f'/api/documents/{doc}/reprocess',json={'expected_revision':99}).status_code==409
    r=client.post(f'/api/documents/{doc}/reprocess',json={'expected_revision':1})
    assert r.status_code==202 and r.json()['revision']==2
    for _ in range(100):
        current=storage[0].document(doc)
        if current['status'] in {'ready','error'}:break
        time.sleep(.05)
    assert current['status']=='ready' and current['revision']==2
    assert {c['revision'] for c in storage[0].chunks(doc)}=={2}
    assert client.post(f'/api/documents/{doc}/reprocess',json={'expected_revision':2}).status_code==409
