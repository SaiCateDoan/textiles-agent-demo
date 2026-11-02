import {test} from 'node:test';
import assert from 'node:assert/strict';
import {newAssistant,applyChatEvent,stopAssistant,readChatEvents} from './chatState.js';

test('visible text can be followed by tools, artifacts and a final message without duplication',()=>{
  let m=newAssistant('local-1');
  const send=(event,value)=>{m=applyChatEvent(m,event,value)};
  send('assistant_delta',{part_id:'p1',delta:'我先检查工况。'});
  assert.equal(m.status,'in_progress');
  send('assistant_message',{part_id:'p1',text:'我先检查工况。',phase:'commentary'});
  send('tool_start',{call_id:'call-1',name:'create_chart',arguments:{}});
  send('tool_result',{call_id:'call-1',name:'create_chart',ok:true,summary:'已计算'});
  send('artifact',{artifact:{id:'chart-1',type:'chart'}});
  send('artifact',{artifact:{id:'chart-1',type:'chart'}});
  send('assistant_delta',{part_id:'p2',delta:'结果如下。'});
  assert.deepEqual(m.parts.map(p=>p.type),['text','tool','artifact','text']);
  assert.equal(m.artifacts.length,1);
  const canonical={...m,status:'completed'};
  send('result',canonical);
  assert.equal(m.content,'我先检查工况。\n\n结果如下。');
  assert.equal(m.parts.length,4);
});

test('stop and errors keep received text, tool results and artifacts',()=>{
  let m=applyChatEvent(newAssistant('x'),'assistant_delta',{part_id:'p',delta:'已找到资料'});
  m=applyChatEvent(m,'tool_start',{call_id:'t',name:'read_document',arguments:{}});
  const stopped=stopAssistant(m);
  assert.equal(stopped.content,'已找到资料');
  assert.equal(stopped.parts[1].status,'interrupted');
  const failed=applyChatEvent(m,'error',{message:'后续调用失败'});
  assert.equal(failed.content,'已找到资料');
  assert.equal(failed.error,'后续调用失败');
});

test('SSE handles split UTF-8, CRLF, keepalive and message completion before later tools',async()=>{
  const wire=': keepalive\r\n\r\nevent: assistant_delta\r\ndata: {"part_id":"p","delta":"纺织"}\r\n\r\nevent: tool_start\r\ndata: {"call_id":"t","name":"search_knowledge","arguments":{}}\r\n\r\nevent: done\r\ndata: {}\r\n\r\n';
  const bytes=new TextEncoder().encode(wire);
  const stream=new ReadableStream({start(controller){for(const byte of bytes)controller.enqueue(new Uint8Array([byte]));controller.close();}});
  const events=[];
  await readChatEvents(stream,(event,value)=>events.push([event,value]));
  assert.deepEqual(events.map(e=>e[0]),['assistant_delta','tool_start','done']);
  assert.equal(events[0][1].delta,'纺织');
});

test('a truncated SSE frame fails instead of pretending the turn completed',async()=>{
  const stream=new ReadableStream({start(c){c.enqueue(new TextEncoder().encode('event: result\ndata: {'));c.close();}});
  await assert.rejects(readChatEvents(stream,()=>{}),/未完成/);
});
