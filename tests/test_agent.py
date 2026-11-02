import json
import itertools
import asyncio
from types import SimpleNamespace

from backend.agent import TextileAgent
from conftest import load_doc


class Message:
    counter=itertools.count()
    def __init__(self,content='',calls=None):
        self.id=f'response-{next(self.counter)}'
        self.status='completed'
        self.output=[]
        if content:
            self.output.append({'type':'message','id':'message-'+self.id,'role':'assistant','status':'completed','content':[{'type':'output_text','text':content}]})
        for call in calls or []:
            self.output.append({'type':'function_call','id':'item-'+call['id'],'call_id':call['id'],'name':call['function']['name'],'arguments':call['function']['arguments']})
    def model_dump(self,**kwargs):
        return {'id':self.id,'status':self.status,'output':self.output}

    def stream(self, terminal='response.completed'):
        events=[]
        for item in self.output:
            if item['type']=='message':
                text=item['content'][0]['text']
                for start in range(0,len(text),3):
                    events.append({'type':'response.output_text.delta','item_id':item['id'],'delta':text[start:start+3]})
        if terminal:
            events.append({'type':terminal,'response':self.model_dump()})
        return Stream(events)


class Stream:
    def __init__(self,events):
        self.events=events;self.closed=False
    async def __aenter__(self):
        return self
    async def __aexit__(self,*args):
        self.closed=True
    async def __aiter__(self):
        for event in self.events:
            await asyncio.sleep(0)
            yield event


async def test_langgraph_executes_artifacts_and_validates_citations(storage,sample_root):
    s,idx=storage;doc=load_doc(storage,sample_root/'02_定型工况实验_模拟.csv')
    calls=[]
    def call(name,args):
        return {'id':f'call_{len(calls)}_{name}','type':'function','function':{'name':name,'arguments':json.dumps(args)}}
    source_ids=[r['id'] for r in s.chunks(doc)]
    responses=[Message(calls=[call('read_document',{'document_id':doc})]),Message(calls=[call('create_chart',dict(option={'series':[{'type':'line','data':[[150,5.1],[180,2.45]]}]},source_ids=source_ids)),call('create_scenario',dict(document_id=doc,parameter='定型温度_C',metric='缩水率_pct'))]),Message('已生成图表与滑块。不存在的引用 [C:invented]')]
    async def create(**kwargs):
        calls.append(kwargs)
        return responses.pop(0).stream()
    a=TextileAgent(s,idx)
    await a.client.close()
    a.client=SimpleNamespace(responses=SimpleNamespace(create=create))
    events=[]
    async def emit(name,data):events.append((name,data))
    result=await a.run('test','分析工况',emit)
    assert {x['type'] for x in result['artifacts']}=={'chart','scenario'}
    assert len(result['sources'])==15 and len(result['trace'])==3
    assert '[C:invented]' not in result['content'] and '未能验证' in result['content']
    assert len(calls)==3 and len(events)>3


async def test_document_scope_enforced_in_tools(storage,sample_root):
    s,idx=storage;doc=load_doc(storage,sample_root/'02_定型工况实验_模拟.csv')
    tool={'id':'call1','type':'function','function':{'name':'read_document','arguments':json.dumps({'document_id':doc})}}
    responses=[Message(calls=[tool]),Message('文档不在检索范围')]
    async def create(**kwargs):return responses.pop(0).stream()
    a=TextileAgent(s,idx);await a.client.close();a.client=SimpleNamespace(responses=SimpleNamespace(create=create))
    async def emit(*args):pass
    result=await a.run('test','读取文档',emit,['not-allowed'])
    assert not result['sources'] and result['trace'][0]['ok'] is False
