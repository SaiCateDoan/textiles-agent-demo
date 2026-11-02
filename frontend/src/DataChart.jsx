import React,{useEffect,useMemo,useRef,useState} from 'react';
import {ArrowUpRight,ChartNoAxesCombined,LoaderCircle} from 'lucide-react';
import {chartDataTables,prepareChartOption} from './chartOptions';

const cell=value=>value==null?'—':typeof value==='object'?JSON.stringify(value):String(value);

export default function DataChart({artifact:a,onSource}){
  const host=useRef(null);
  const [loading,setLoading]=useState(true),[error,setError]=useState('');
  const signature=JSON.stringify(a.option||{data:a.data,chart_type:a.chart_type,group_by:a.group_by,metric:a.metric});
  const option=useMemo(()=>prepareChartOption(a),[signature]);
  const tables=useMemo(()=>chartDataTables(option),[option]);
  useEffect(()=>{
    let disposed=false,chart,observer,frame;
    setLoading(true);setError('');
    // Load the plotting engine only when a chart is actually displayed.
    import('echarts').then(engine=>{
      if(disposed||!host.current)return;
      chart=engine.init(host.current,null,{renderer:'canvas'});
      chart.setOption(option,{notMerge:true});
      const resize=()=>{cancelAnimationFrame(frame);frame=requestAnimationFrame(()=>chart?.resize());};
      observer=new ResizeObserver(resize);observer.observe(host.current);
      setLoading(false);
    }).catch(()=>{if(!disposed){setLoading(false);setError('图表暂时无法绘制，可查看下方数据或重新生成。');}});
    return()=>{disposed=true;observer?.disconnect();cancelAnimationFrame(frame);chart?.dispose();};
  },[option]);
  const sources=a.source_ids||[];
  return <section className="artifact flexible-chart">
    <div className="artifact-head"><span className="square-icon"><ChartNoAxesCombined size={17}/></span><div><b>{a.title}</b><small>{a.option?`据 ${sources.length} 个来源文段整理`:`${({mean:'算术平均',sum:'合计',min:'最小值',max:'最大值',count:'有效数值行数'})[a.aggregation]||'历史图表'} · ${a.total_rows||0} 行原始数据`}{a.revision?` · v${a.revision}`:''}</small></div><span className="tag">来源可追溯</span></div>
    {a.description&&<p className="chart-description">{a.description}</p>}
    <div className="chart-stage"><div ref={host} className="chart-canvas" role="img" aria-label={a.title||'数据图表'}/>{loading&&<div className="chart-loading"><LoaderCircle size={17} className="spin"/>正在绘制图表</div>}{error&&<div className="inline-error chart-render-error">{error}</div>}</div>
    <div className="artifact-foot chart-foot">
      {onSource&&sources.length>0&&<div className="chart-source-links">{sources.map(id=><button className="source-chip" key={id} onClick={()=>onSource(id)}>来源 {id.slice(0,6)} <ArrowUpRight size={10}/></button>)}</div>}
      {tables.length>0&&<details><summary>查看图表数据</summary>{tables.map((data,i)=><div className="chart-table" key={i}><b>{data.name}</b><table><thead><tr>{data.headers.map((h,j)=><th key={j}>{cell(h)}</th>)}</tr></thead><tbody>{data.rows.slice(0,200).map((row,j)=><tr key={j}>{data.headers.map((_,k)=><td key={k}>{cell(row[k])}</td>)}</tr>)}</tbody></table>{data.rows.length>200&&<p>展示前 200 行，共 {data.rows.length} 行。</p>}</div>)}</details>}
    </div>
  </section>;
}
