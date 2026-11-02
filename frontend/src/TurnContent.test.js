import {test} from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {createServer} from 'vite';

test('tool progress disappears into continuous reply text while artifacts stay in order',async()=>{
  // Compile repository JSX in memory. No browser/page access or HTTP listener.
  const server=await createServer({cacheDir:'.cache/ssr-tests',optimizeDeps:{noDiscovery:true,include:[]},server:{middlewareMode:true,hmr:false,watch:null},appType:'custom'});
  try{
    const {default:TurnContent}=await server.ssrLoadModule('/src/TurnContent.jsx');
    const message={content:'先说明\n\n最后结论',status:'completed',sources:[],artifacts:[{id:'a',type:'chart'}],parts:[
      {id:'p1',type:'text',text:'先说明',phase:'commentary'},
      {id:'t1',type:'tool',name:'search_knowledge',status:'completed',summary:'检索完成'},
      {id:'a1',type:'artifact',artifact_id:'a'},
      {id:'p2',type:'text',text:'最后结论',phase:'final'},
    ]};
    const html=renderToStaticMarkup(React.createElement(TurnContent,{message,toolNames:{search_knowledge:'查询资料'},onSource:()=>{},renderArtifact:()=>React.createElement('div',null,'图表已生成')}));
    const positions=['先说明','图表已生成','最后结论'].map(text=>html.indexOf(text));
    assert.ok(positions.every(p=>p>=0));
    assert.deepEqual([...positions].sort((a,b)=>a-b),positions);
    assert.equal(html.split('图表已生成').length-1,1);
    assert.ok(!html.includes('查询资料')&&!html.includes('tool-progress'));
    const render=message=>renderToStaticMarkup(React.createElement(TurnContent,{message,live:true,toolNames:{search_knowledge:'查询资料',read_document:'读取原文'},onSource:()=>{},renderArtifact:()=>null}));
    const first={id:'p1',type:'text',text:'先说明',phase:'commentary'};
    const running={id:'t1',type:'tool',name:'search_knowledge',status:'running'};
    const final={id:'p2',type:'text',text:'后续回复',phase:'pending'};
    const active=render({status:'in_progress',parts:[first,running]});
    assert.ok(active.includes('tool-progress')&&active.includes('查询资料')&&!active.includes('<details'));
    const resumed=render({status:'in_progress',parts:[first,{...running,status:'completed'},final]});
    const continuous=render({status:'in_progress',parts:[first,final]});
    assert.equal(resumed,continuous);
    assert.deepEqual(first,{id:'p1',type:'text',text:'先说明',phase:'commentary'});
    assert.equal(render({status:'in_progress',parts:[first,running,final]}),continuous);
    const next=render({status:'in_progress',parts:[first,{...running,status:'completed'},{...running,id:'t2',name:'read_document'}]});
    assert.ok(next.includes('读取原文')&&!next.includes('查询资料'));
    assert.ok(!render({status:'interrupted',parts:[first,running]}).includes('tool-progress'));
    const old=renderToStaticMarkup(React.createElement(TurnContent,{message:{content:'历史回答',artifacts:[],sources:[]},toolNames:{},onSource:()=>{},renderArtifact:()=>null}));
    assert.ok(old.includes('历史回答'));
  }finally{await server.close();}
});
