import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {AlertCircle,Square} from 'lucide-react';
import {RetrievedMedia} from './Passage';

export function MarkdownText({text='',onSource,isError=false}){
  return <div className={isError?'markdown error-text':'markdown'}><ReactMarkdown remarkPlugins={[remarkGfm]} components={{a:({href,children})=>href?.startsWith('#source:')?<button className="citation" onClick={()=>onSource(href.slice(8))}>{children}</button>:<a href={href} target="_blank" rel="noreferrer">{children}</a>}}>{text.replace(/\[C:([^\]]+)\]/g,(_,id)=>`[↗ ${id.slice(0,5)}](#source:${id})`)}</ReactMarkdown></div>;
}

export default function TurnContent({message:m,onSource,renderArtifact,toolNames,live=false}){
  const timeline=Array.isArray(m.parts)&&m.parts.length>0;
  const artifacts=new Map((m.artifacts||[]).map(a=>[a.id,a]));
  const parts=[];
  let activeTool=-1;
  if(live&&m.status==='in_progress'){
    for(let i=(m.parts||[]).length-1;i>=0;i--){
      const part=m.parts[i];
      if((part.type==='text'&&part.text)||part.type==='artifact')break;
      if(part.type==='tool'&&part.status==='running'){activeTool=i;break;}
    }
  }
  // Hidden tool records must leave no wrapper or extra paragraph spacing.
  // Merge adjacent public text for the same Markdown flow after a tool finishes.
  for(const [i,part] of (m.parts||[]).entries()){
    if(part.type==='tool'){
      if(i===activeTool)parts.push(part);
    }else if(part.type==='text'&&part.text){
      const previous=parts.at(-1);
      if(previous?.type==='text')previous.text+='\n\n'+part.text;
      else parts.push({...part});
    }else if(part.type==='artifact'&&artifacts.has(part.artifact_id))parts.push(part);
  }
  return <>
    {timeline?parts.map(part=>{
      if(part.type==='text')return <div className="response-text" key={part.id}><MarkdownText text={part.text} onSource={onSource}/></div>;
      if(part.type==='artifact'){const a=artifacts.get(part.artifact_id);return a?<React.Fragment key={part.id}>{renderArtifact(a)}</React.Fragment>:null;}
      if(part.type==='tool')return <div className="tool-progress" role="status" key={part.id}><span>正在调用：{toolNames[part.name]||part.name}</span></div>;
      return null;
    }):<><MarkdownText text={m.content} onSource={onSource} isError={m.isError}/>{m.artifacts?.map(a=><React.Fragment key={a.id}>{renderArtifact(a)}</React.Fragment>)}</>}
    <RetrievedMedia message={{...m,content:m.content||''}} onSource={onSource}/>
    {['interrupted','failed'].includes(m.status)&&<div className="turn-notice" role="status">{m.status==='interrupted'?<Square size={13}/>:<AlertCircle size={14}/>}<span>{m.error||(m.status==='interrupted'?'已停止，已收到的内容保留在此。':'回复未完成，请重试。')}</span></div>}
    {m.status==='in_progress'&&!live&&<div className="turn-notice">这轮回复尚未完成，已收到的内容保留在此。</div>}
  </>;
}
