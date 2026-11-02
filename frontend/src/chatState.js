// The response turn outlives individual text messages and vendor responses.
export function newAssistant(clientTurnId) {
  return {id:clientTurnId,clientTurnId,role:'assistant',content:'',parts:[],artifacts:[],sources:[],trace:[],status:'in_progress',api:'responses'};
}

export function applyChatEvent(message,event,value) {
  if(event==='turn_start')return {...message,id:value.message_id};
  if(event==='result'||event==='turn_snapshot')return {...message,...value,role:'assistant',clientTurnId:message.clientTurnId};
  if(event==='error')return {...message,status:'failed',error:value.message};
  if(event==='model')return {...message,model:value.model};
  if(event==='vision_images')return {...message,vision_images_used:value.ids};
  let parts=[...(message.parts||[])];
  const update=(id,create,change)=>{const i=parts.findIndex(p=>p.id===id);if(i<0)parts.push(change(create));else parts[i]=change(parts[i]);};
  if(['assistant_start','assistant_delta','assistant_message'].includes(event)){
    update(value.part_id,{type:'text',id:value.part_id,text:'',phase:'pending',status:'in_progress'},part=>event==='assistant_delta'?{...part,text:part.text+value.delta}:event==='assistant_message'?{...part,text:value.text,phase:value.phase,status:'completed'}:part);
    return {...message,parts,content:parts.filter(p=>p.type==='text').map(p=>p.text).filter(Boolean).join('\n\n')};
  }
  if(event==='tool_start'){
    update(value.call_id,{type:'tool',id:value.call_id,name:value.name,arguments:value.arguments,status:'running'},part=>part);
    return {...message,parts};
  }
  if(event==='tool_result'){
    update(value.call_id,{type:'tool',id:value.call_id,name:value.name,arguments:value.arguments},part=>({...part,status:value.ok?'completed':'failed',summary:value.summary}));
    return {...message,parts,trace:[...(message.trace||[]),value]};
  }
  if(event==='sources'){
    const sources=new Map((message.sources||[]).map(s=>[s.id,s]));
    value.sources.forEach(s=>sources.set(s.id,s));
    return {...message,sources:[...sources.values()]};
  }
  if(event==='artifact'){
    const artifacts=[...(message.artifacts||[])];
    if(!artifacts.some(a=>a.id===value.artifact.id)){
      artifacts.push(value.artifact);
      parts.push({type:'artifact',id:'part-'+value.artifact.id,artifact_id:value.artifact.id});
    }
    return {...message,parts,artifacts};
  }
  return message;
}

export function stopAssistant(message,status='interrupted',error) {
  return {...message,status,error,parts:(message.parts||[]).map(p=>['running','in_progress'].includes(p.status)?{...p,status:status==='interrupted'?'interrupted':'failed'}:p)};
}

export async function readChatEvents(body,onEvent) {
  const reader=body.getReader(),decoder=new TextDecoder();
  let buffer='';
  const drain=()=>{
    buffer=buffer.replaceAll('\r\n','\n');
    let boundary;
    while((boundary=buffer.indexOf('\n\n'))!==-1){
      const frame=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2);
      const lines=frame.split('\n');
      const event=lines.find(l=>l.startsWith('event:'))?.slice(6).trim();
      const data=lines.filter(l=>l.startsWith('data:')).map(l=>l.slice(5).trimStart()).join('\n');
      if(data)onEvent(event,JSON.parse(data));
    }
  };
  try{
    while(true){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});drain();}
    buffer+=decoder.decode();drain();
    if(buffer.trim())throw new Error('回复数据传输未完成，请重试');
  }finally{
    await reader.cancel().catch(()=>{});
    reader.releaseLock();
  }
}
