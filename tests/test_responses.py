import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from backend.agent import TextileAgent
from backend.responses import CitationBuffer, ResponseStreamError
from backend.turns import ProgressWriter
from conftest import load_doc
from test_agent import Message, Stream
from test_api import client


def tool(call_id,name,**args):
    return {'id':call_id,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}


async def agent_with_responses(storage,responses):
    agent=TextileAgent(*storage)
    await agent.client.close()
    requests=[]
    async def create(**options):
        requests.append(options)
        return responses.pop(0).stream()
    agent.client=SimpleNamespace(responses=SimpleNamespace(create=create))
    return agent,requests


async def test_commentary_tools_artifacts_final_are_streamed_and_preserved_in_order(storage,sample_root,monkeypatch):
    store,index=storage
    doc=load_doc(storage,sample_root/'02_定型工况实验_模拟.csv')
    from backend.datasets import list_datasets
    dataset=list_datasets(store,doc)[0]
    citation='f'*16
    monkeypatch.setattr('backend.datasets.uid',lambda:citation)
    responses=[Message('我先确认资料。',[tool('t1','query_datasets',dataset_ids=[dataset['id']],sql='SELECT "定型温度_C", AVG("缩水率_pct") AS "均值" FROM data GROUP BY "定型温度_C" ORDER BY "定型温度_C"',description='按温度计算缩水率算术均值')]),
               Message('已找到实验表，接着计算图表和控件。',[
                   tool('t2','create_data_chart',query_id=citation,x='定型温度_C',y=['均值'],chart_type='line'),
                   tool('t3','create_scenario',document_id=doc,parameter='定型温度_C',metric='缩水率_pct')]),
               Message(f'结果仅供历史工况参考。[C:{citation}]')]
    agent,requests=await agent_with_responses(storage,responses)
    events=[]
    async def emit(event,value):events.append((event,value))
    result=await agent.run('new-chat','生成图表及控件',emit)
    assert result['status']=='completed' and result['api']=='responses'
    assert [p['type'] for p in result['parts']]==['text','tool','text','tool','artifact','tool','artifact','text']
    assert [p['phase'] for p in result['parts'] if p['type']=='text']==['commentary','commentary','final']
    assert result['content'].startswith('我先确认资料。') and '接着计算' in result['content']
    assert result['content'].endswith(f'[C:{citation}]')
    assert next(i for i,e in enumerate(events) if e[0]=='assistant_delta') < next(i for i,e in enumerate(events) if e[0]=='tool_start')
    assert next(i for i,e in enumerate(events) if e[0]=='artifact') < max(i for i,e in enumerate(events) if e[0]=='assistant_start')
    assert all(r['stream'] and r['reasoning']=={'effort':'none'} and 'previous_response_id' not in r for r in requests)
    assert 'list_documents' in {t['name'] for t in requests[0]['tools']} and 'function' not in requests[0]['tools'][0]
    second=requests[1]['input']
    assert any(i['type']=='message' and i.get('role')=='assistant' for i in second)
    calls={i['call_id'] for i in second if i['type']=='function_call'}
    outputs={i['call_id'] for i in second if i['type']=='function_call_output'}
    assert calls==outputs=={'t1'}


async def test_incomplete_response_does_not_execute_partial_tools_and_closes_stream(storage):
    stream=Message('我先检查。',[tool('t1','list_documents')]).stream('response.incomplete')
    agent=TextileAgent(*storage);await agent.client.close()
    async def create(**kwargs):return stream
    agent.client=SimpleNamespace(responses=SimpleNamespace(create=create))
    events=[]
    async def emit(name,value):events.append((name,value))
    with pytest.raises(ResponseStreamError):
        await agent.run('test','继续',emit)
    assert stream.closed and any(e[0]=='assistant_delta' for e in events)
    assert not any(e[0]=='tool_start' for e in events)


async def test_missing_terminal_event_is_failure_not_final_answer(storage):
    stream=Message('只收到一部分').stream(None)
    agent=TextileAgent(*storage);await agent.client.close()
    async def create(**kwargs):return stream
    agent.client=SimpleNamespace(responses=SimpleNamespace(create=create))
    async def emit(*args):pass
    with pytest.raises(ResponseStreamError):await agent.run('test','继续',emit)
    assert stream.closed


async def test_reasoning_never_enters_visible_timeline_or_result(storage):
    message=Message('公开答复')
    events=[{'type':'response.reasoning_text.delta','delta':'PRIVATE_CHAIN'}]
    events+=message.stream().events
    stream=Stream(events)
    agent=TextileAgent(*storage);await agent.client.close()
    async def create(**kwargs):return stream
    agent.client=SimpleNamespace(responses=SimpleNamespace(create=create))
    visible=[]
    async def emit(name,value):visible.append((name,value))
    result=await agent.run('test','你好',emit)
    assert 'PRIVATE_CHAIN' not in json.dumps([visible,result])


async def test_cancel_closes_active_vendor_stream(storage):
    started=asyncio.Event()
    class Hanging(Stream):
        async def __aiter__(self):
            yield {'type':'response.output_text.delta','item_id':'m1','delta':'收到'}
            started.set()
            await asyncio.Event().wait()
    stream=Hanging([])
    agent=TextileAgent(*storage);await agent.client.close()
    async def create(**kwargs):return stream
    agent.client=SimpleNamespace(responses=SimpleNamespace(create=create))
    async def emit(*args):pass
    running=asyncio.create_task(agent.run('test','你好',emit))
    await asyncio.wait_for(started.wait(),2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):await running
    assert stream.closed


def test_split_citation_is_never_rendered_as_an_unverified_link():
    guard=CitationBuffer({'allowed':{}})
    assert guard.feed('内容[C:')=='内容'
    assert guard.feed('invented')==''
    assert guard.feed(']。')=='[来源未验证]。'
    assert guard.feed('[C:allowed]')=='[C:allowed]'


async def test_final_snapshot_cannot_be_overwritten_by_a_slow_progress_write():
    entered,release=threading.Event(),threading.Event()
    writes=[]
    class Store:
        def update_message(self,message_id,payload):
            if not writes:
                entered.set();assert release.wait(3)
            writes.append(payload)
    writer=ProgressWriter(Store(),'m','test',interval=.01)
    writer.apply('assistant_delta',{'part_id':'p','delta':'第一段'})
    writer.start()
    while not entered.is_set():await asyncio.sleep(.005)
    writer.apply('assistant_delta',{'part_id':'p','delta':'继续'})
    finish=asyncio.create_task(writer.finish('interrupted'))
    await asyncio.sleep(.02)
    assert not finish.done()
    release.set();await asyncio.wait_for(finish,3)
    assert writes[-1]['status']=='interrupted' and writes[-1]['content']=='第一段继续'


def test_api_error_keeps_partial_reply_and_one_assistant_row(client,storage,monkeypatch):
    from backend import main
    class Broken:
        client=main.agent.client
        async def run(self,chat_id,question,emit,scope):
            await emit('assistant_delta',{'part_id':'m','delta':'我已找到工艺说明。'})
            await emit('tool_start',{'call_id':'c','name':'read_document','arguments':{}})
            raise RuntimeError('VENDOR_SECRET_DO_NOT_FORWARD')
    monkeypatch.setattr(main,'agent',Broken())
    monkeypatch.setattr(main.settings,'deepseek_api_key','test-only')
    chat=client.post('/api/chats').json()['id']
    response=client.post(f'/api/chats/{chat}/stream',json={'content':'继续查询'})
    assert 'event: assistant_delta' in response.text and 'event: error' in response.text and 'event: done' in response.text
    assert 'VENDOR_SECRET' not in response.text
    rows=storage[0].messages(chat)
    assert len(rows)==2 and rows[-1]['status']=='failed'
    assert rows[-1]['content']=='我已找到工艺说明。'
    assert rows[-1]['parts'][1]['status']=='failed'


def test_api_timeout_preserves_text(client,storage,monkeypatch):
    from backend import main
    class Slow:
        client=main.agent.client
        async def run(self,chat_id,question,emit,scope):
            await emit('assistant_delta',{'part_id':'m','delta':'先给出阶段说明。'})
            await asyncio.sleep(10)
    monkeypatch.setattr(main,'agent',Slow())
    monkeypatch.setattr(main.settings,'deepseek_api_key','test-only')
    monkeypatch.setattr(main.settings,'chat_timeout_seconds',.05)
    chat=client.post('/api/chats').json()['id']
    response=client.post(f'/api/chats/{chat}/stream',json={'content':'继续查询'})
    assert 'event: done' in response.text
    assert storage[0].messages(chat)[-1]['content']=='先给出阶段说明。'
    assert storage[0].messages(chat)[-1]['status']=='failed'


async def test_disconnect_persists_partial_reply_and_releases_chat_lock(storage,monkeypatch):
    from backend import main
    original_client=main.agent.client
    started=asyncio.Event()
    class Slow:
        client=original_client
        async def run(self,chat_id,question,emit,scope):
            await emit('assistant_delta',{'part_id':'p','delta':'收到，正在检查。'})
            started.set();await asyncio.Event().wait()
    monkeypatch.setattr(main,'store',storage[0]);monkeypatch.setattr(main,'index',storage[1])
    monkeypatch.setattr(main,'agent',Slow());monkeypatch.setattr(main.settings,'deepseek_api_key','test-only')
    async with main.lifespan(main.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as http:
            chat=(await http.post('/api/chats')).json()['id']
            request=asyncio.create_task(http.post(f'/api/chats/{chat}/stream',json={'content':'继续'}))
            await asyncio.wait_for(started.wait(),2)
            assert (await http.post(f'/api/chats/{chat}/stream',json={'content':'并发发送'})).status_code==409
            request.cancel()
            with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(request,3)
            deadline=asyncio.get_running_loop().time()+3
            while storage[0].messages(chat)[-1]['status']=='in_progress' and asyncio.get_running_loop().time()<deadline:
                await asyncio.sleep(.02)
            rows=storage[0].messages(chat)
            assert rows[-1]['status']=='interrupted' and rows[-1]['content']=='收到，正在检查。'
            assert chat not in main.chat_locks


async def test_vision_uses_responses_json_mode_with_input_image():
    from backend.multimodal import Candidate, DeepSeekVision
    from test_multimodal import png,FakeVision
    image=Candidate('a'*16,png(),'图片1',180,120,'standalone_image',True)
    decision=await FakeVision().analyze(image,'邻文')
    requests=[]
    async def create(**kwargs):
        requests.append(kwargs)
        return Message(json.dumps(decision))
    vision=DeepSeekVision(SimpleNamespace(responses=SimpleNamespace(create=create)),'fixture')
    result=await vision.analyze(image,'邻文')
    assert result.category=='fabric'
    request=requests[0]
    assert request['text']=={'format':{'type':'json_object'}}
    assert request['input'][0]['content'][1]['type']=='input_image'


async def test_every_call_has_output_when_model_exceeds_per_step_budget(storage):
    agent,requests=await agent_with_responses(storage,[Message(calls=[tool(f't{i}','list_documents') for i in range(10)]),Message('工具检查结束')])
    async def emit(*args):pass
    result=await agent.run('test','检查',emit)
    replay=requests[1]['input']
    calls={i['call_id'] for i in replay if i['type']=='function_call'}
    outputs={i['call_id'] for i in replay if i['type']=='function_call_output'}
    assert calls==outputs and len(outputs)==10 and len(result['trace'])==8


async def test_duplicate_call_id_is_rejected_before_reexecution(storage):
    call=tool('same-id','list_documents')
    agent,_=await agent_with_responses(storage,[Message(calls=[call]),Message(calls=[call])])
    events=[]
    async def emit(name,value):events.append((name,value))
    with pytest.raises(ResponseStreamError):await agent.run('test','检查',emit)
    assert sum(name=='tool_start' for name,_ in events)==1


async def test_final_round_disables_tools_and_returns_final_text(storage,monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings,'chat_max_rounds',2)
    agent,requests=await agent_with_responses(storage,[Message('先检查',[tool('t','list_documents')]),Message('预算内完成')])
    async def emit(*args):pass
    result=await agent.run('test','检查',emit)
    assert requests[-1]['tools']==[] and requests[-1]['tool_choice']=='none'
    assert result['parts'][-1]['phase']=='final'


async def test_completed_event_finishes_without_waiting_for_vendor_eof(storage):
    message=Message('已完成')
    class HangingAfterCompletion(Stream):
        async def __aiter__(self):
            yield {'type':'response.completed','response':message.model_dump()}
            await asyncio.Event().wait()
    stream=HangingAfterCompletion([])
    agent=TextileAgent(*storage);await agent.client.close()
    async def create(**kwargs):return stream
    agent.client=SimpleNamespace(responses=SimpleNamespace(create=create))
    async def emit(*args):pass
    result=await asyncio.wait_for(agent.run('test','检查',emit),2)
    assert stream.closed and result['content']=='已完成'
