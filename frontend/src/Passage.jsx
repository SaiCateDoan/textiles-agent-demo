import React, {useState} from 'react';
import {ArrowUpRight, Image as ImageIcon} from 'lucide-react';

function Picture({image:im}) {
  const [failed,setFailed]=useState(false);
  const valid=/^\/api\/images\/[a-f0-9]{16}$/.test(im.url||'');
  return <figure className="passage-image" data-image-id={im.id}>
    {valid&&!failed?<a href={im.url} target="_blank" rel="noreferrer" title="查看原图"><img src={im.preview_url||im.url} alt={im.description||'原文附图'} loading="lazy" onError={()=>setFailed(true)}/><span className="picture-open">查看原图 <ArrowUpRight size={13}/></span></a>:<div className="image-unavailable"><ImageIcon size={22}/>原图已移除或暂不可用</div>}
    <figcaption><b>视觉描述 · 模型生成</b><p>{im.description}</p><small>{im.locator} · {im.category==='fabric'?'织物外观':im.category==='chart'?'图表':'原文配图'}{im.confidence<.75?' · 建议人工复核':''}</small></figcaption>
  </figure>;
}

export function Passage({text='',images=[]}) {
  const byId=new Map(images.map(im=>[im.id,im]));
  const pieces=[];let cursor=0;
  for(const match of text.matchAll(/\[\[IMG:([a-f0-9]{16})\]\]/g)) {
    if(!byId.has(match[1]))continue;
    if(match.index>cursor)pieces.push(<p className="passage-text" key={`text-${cursor}`}>{text.slice(cursor,match.index)}</p>);
    pieces.push(<Picture key={`${match[1]}-${match.index}`} image={byId.get(match[1])}/>);
    cursor=match.index+match[0].length;
  }
  if(cursor<text.length)pieces.push(<p className="passage-text" key={`text-${cursor}`}>{text.slice(cursor)}</p>);
  return <div className="passage">{pieces}</div>;
}

export function RetrievedMedia({message,onSource}) {
  const cited=new Set([...message.content.matchAll(/\[C:([^\]]+)\]/g)].map(m=>m[1]));
  const sources=(message.sources||[]).filter(s=>cited.has(s.id)&&s.images?.length);
  if(!sources.length)return null;
  return <div className="retrieved-media">{sources.map(s=><section key={s.id}><button className="media-source" onClick={()=>onSource(s.id)}>原文附图 · {s.document_name} · {s.locator}<ArrowUpRight size={13}/></button><div className="media-grid">{s.images.map(im=><Picture key={im.id} image={im}/>)}</div></section>)}</div>;
}
