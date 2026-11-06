import hashlib
import time

from test_api import client


def test_idempotent_upload_and_conflicting_retry_key(client,storage):
    headers={"Idempotency-Key":"client-retry-001"}
    data="纺织同一份上传".encode()
    a=client.post('/api/documents',headers=headers,files={'file':('one.txt',data)})
    b=client.post('/api/documents',headers=headers,files={'file':('one.txt',data)})
    assert a.status_code==b.status_code==202 and a.json()['id']==b.json()['id']
    assert storage[0].document_count()==1
    assert len(list((storage[0].root/'uploads').glob('*')))==1
    assert a.json()['content_hash']==hashlib.sha256(data).hexdigest()
    conflict=client.post('/api/documents',headers=headers,files={'file':('one.txt',b'different')})
    assert conflict.status_code==409 and storage[0].document_count()==1


def test_queue_backpressure_does_not_leave_orphan_upload(client,storage,monkeypatch):
    from backend import main
    runner=main.app.state.ingestion
    client.portal.call(runner.stop)
    monkeypatch.setattr(main.settings,'ingestion_queue_limit',1)
    a=client.post('/api/documents',files={'file':('one.txt',b'first')})
    b=client.post('/api/documents',files={'file':('two.txt',b'second')})
    assert a.status_code==202 and b.status_code==503
    assert b.headers['Retry-After']=='10'
    assert len(list((storage[0].root/'uploads').glob('*')))==1
    assert client.get('/api/health').json()['ingestion']['queued']==1
    job=client.get('/api/documents/'+a.json()['id']+'/job').json()
    assert job['state']=='queued' and 'owner' not in job


def test_paginated_documents_and_chunks(client,storage):
    from backend import main
    from backend.store import now
    client.portal.call(main.app.state.ingestion.stop)
    store=storage[0]
    with store.connect() as c:
        c.executemany("INSERT INTO documents(id,name,suffix,size,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            [(f'{i:016x}',f'测试资料{i:04d}.txt','.txt',10,'ready',now(),now()) for i in range(205)])
    first=client.get('/api/documents/page?limit=30').json()
    second=client.get('/api/documents/page?limit=30&offset=30').json()
    assert first['total']==205 and first['next_offset']==30
    assert len(first['items'])==len(second['items'])==30
    assert not ({d['id'] for d in first['items']}&{d['id'] for d in second['items']})
    assert client.get('/api/documents/page?q=0204').json()['total']==1
    assert len(client.get('/api/documents').json())==100
    assert client.get('/api/documents?limit=1001').status_code==422
    doc_id='0000000000000000'
    rows=[dict(id=f'{i+1000:016x}',text=f'纺织分块{i}',locator=f'行{i}',kind='text',revision=1) for i in range(121)]
    store.replace_chunks(doc_id,rows)
    assert len(client.get(f'/api/documents/{doc_id}/chunks?limit=50&offset=50').json())==50
    assert client.get(f'/api/documents/{doc_id}/chunks?limit=50&offset=100').json()[0]['text']=='纺织分块100'


def test_failed_edit_keeps_active_version(client,storage,sample_root,monkeypatch):
    from conftest import load_doc
    store,index=storage
    doc_id=load_doc(storage,sample_root/'10_文章图表数据_模拟.txt')
    row=store.chunks(doc_id)[0]
    original=index.stage
    def broken(*args):
        original(*args)
        raise OSError('simulated disk write interruption')
    monkeypatch.setattr(index,'stage',broken)
    response=client.put('/api/chunks/'+row['id'],json={'revision':1,'text':row['text'].replace('1.2','3.2')})
    assert response.status_code==503
    doc=store.document(doc_id)
    assert doc['status']=='ready' and doc['active_revision']==1
    assert store.chunks(doc_id)[0]['text']==row['text']
    assert index.search(store,'定型A 不良率',[doc_id])[0]['revision']==1
