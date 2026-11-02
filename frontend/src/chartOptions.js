const list=value=>value==null?[]:Array.isArray(value)?value:[value];

export function prepareChartOption(artifact){
  // Display old saved chart artifacts; new tools only accept the flexible option.
  const input=artifact.option||{xAxis:{type:'category',name:artifact.group_by,data:(artifact.data||[]).map(d=>d.name)},yAxis:{type:'value',name:artifact.metric},series:[{type:artifact.chart_type||'bar',name:artifact.metric,data:(artifact.data||[]).map(d=>d.value)}]};
  const clean=(value,path=[])=>{
    if(Array.isArray(value))return value.map((v,i)=>clean(v,[...path,String(i)]));
    if(value&&typeof value==='object'){
      const out={};
      for(const [key,item] of Object.entries(value)){
        if(['__proto__','constructor','prototype','link','sublink','image','dataView'].includes(key))continue;
        if(key==='reg'&&path.includes('transform'))continue;
        out[key]=clean(item,[...path,key]);
      }
      const context=[...path].reverse().find(p=>!/^\d+$/.test(p));
      if(context==='tooltip'){out.renderMode='richText';out.confine=true;delete out.extraCssText;}
      if(context==='saveAsImage'){out.name='textile-chart';out.type='png';}
      return out;
    }
    if(typeof value==='string'&&value.toLowerCase().startsWith('image://'))return 'circle';
    return value;
  };
  const option=clean(input);
  const cartesian=list(option.series).filter(s=>(s.coordinateSystem==null||s.coordinateSystem==='cartesian2d')&&['bar','line','scatter','effectScatter','candlestick','heatmap','pictorialBar','boxplot'].includes(s.type));
  if(cartesian.length){
    const paired=cartesian.map(s=>(s.data||[]).find(p=>p!=null)).filter(Boolean);
    const numeric=cartesian.every(s=>['scatter','effectScatter'].includes(s.type))||(paired.length>0&&paired.every(p=>Array.isArray(p)&&typeof p[0]==='number'));
    const temporal=paired.length>0&&paired.every(p=>Array.isArray(p)&&typeof p[0]==='string'&&/^\d{4}-\d{2}-\d{2}/.test(p[0]));
    if(option.xAxis==null)option.xAxis={type:option.yAxis?.type==='category'||numeric?'value':temporal?'time':'category'};
    option.yAxis??={type:'value'};
    if(!option.dataset&&!Array.isArray(option.xAxis)&&option.xAxis.type==='category'&&!option.xAxis.data){
      const data=cartesian[0].data||[];
      option.xAxis.data=data.map((point,i)=>point?.name||(Array.isArray(point)?point[0]:String(i+1)));
    }
  }
  option.animation=false;
  option.color??=['#227d67','#6fb6a3','#c29a58','#748dc0','#b47486','#74a4a8','#9b9272'];
  option.textStyle={fontFamily:'Microsoft YaHei, sans-serif',fontSize:14,...option.textStyle};
  option.tooltip??={renderMode:'richText',confine:true};
  option.aria={...option.aria,enabled:true};
  for(const key of ['xAxis','yAxis','radiusAxis','angleAxis','singleAxis','parallelAxis']){
    for(const axis of list(option[key]))if(axis&&typeof axis==='object')axis.axisLabel={fontSize:14,...axis.axisLabel};
  }
  return option;
}

function table(source,dimensions,header){
  if(source&&typeof source==='object'&&!Array.isArray(source)){
    const keys=Object.keys(source);
    if(keys.every(k=>Array.isArray(source[k]))){
      const length=Math.max(0,...keys.map(k=>source[k].length));
      return {headers:keys,rows:Array.from({length},(_,i)=>keys.map(k=>source[k][i]))};
    }
  }
  if(!Array.isArray(source)||!source.length)return null;
  const names=list(dimensions).map(d=>typeof d==='string'?d:d.name);
  if(Array.isArray(source[0])){
    const hasHeader=header===true||(header!==false&&!names.length&&source[0].every(v=>typeof v==='string'));
    const headers=names.length?names:hasHeader?source[0]:source[0].map((_,i)=>`维度 ${i+1}`);
    return {headers,rows:hasHeader?source.slice(1):source};
  }
  if(source[0]&&typeof source[0]==='object'){
    const keys=[...new Set(source.flatMap(row=>Object.keys(row||{})))];
    return {headers:keys,rows:source.map(row=>keys.map(k=>row?.[k]))};
  }
  return {headers:['序号',names[0]||'数值'],rows:source.map((v,i)=>[i+1,v])};
}

export function chartDataTables(option){
  const tables=[];
  list(option.dataset).forEach((dataset,i)=>{
    const data=table(dataset.source,dataset.dimensions,dataset.sourceHeader);
    if(data)tables.push({name:dataset.name||`数据集 ${i+1}`,...data});
  });
  list(option.series).forEach((series,i)=>{
    const data=table(series.data||series.nodes,series.dimensions,false);
    if(data)tables.push({name:series.name||`系列 ${i+1}`,...data});
  });
  return tables;
}
