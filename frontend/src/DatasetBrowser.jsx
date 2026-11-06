import React,{useEffect,useState} from 'react';

export default function DatasetBrowser({documentId,revision}){
  const [datasets,setDatasets]=useState([]),[selected,setSelected]=useState(null),[offset,setOffset]=useState(0),[page,setPage]=useState(null),[error,setError]=useState(''),[loading,setLoading]=useState(false);
  const [catalogOffset,setCatalogOffset]=useState(0),[more,setMore]=useState(false),[catalogLoading,setCatalogLoading]=useState(false);
  useEffect(()=>{
    setDatasets([]);setSelected(null);setPage(null);setError('');setCatalogOffset(0);setMore(false);
  },[documentId,revision]);
  useEffect(()=>{
    const ctrl=new AbortController();setCatalogLoading(true);
    fetch(`/api/documents/${documentId}/datasets?offset=${catalogOffset}`,{signal:ctrl.signal}).then(r=>{if(!r.ok)throw new Error('数据集目录暂不可用');return r.json()}).then(items=>{setDatasets(previous=>catalogOffset?[...previous,...items]:items);setMore(items.length===50)}).catch(e=>{if(e.name!=='AbortError')setError(e.message)}).finally(()=>{if(!ctrl.signal.aborted)setCatalogLoading(false)});
    return()=>ctrl.abort();
  },[documentId,revision,catalogOffset]);
  useEffect(()=>{
    if(!selected)return;
    const ctrl=new AbortController();setLoading(true);setError('');setPage(null);
    fetch(`/api/datasets/${selected}/preview?offset=${offset}`,{signal:ctrl.signal}).then(async r=>{const body=await r.json();if(!r.ok)throw new Error(body.detail||'预览失败');return body}).then(setPage).catch(e=>{if(e.name!=='AbortError')setError(e.message)}).finally(()=>{if(!ctrl.signal.aborted)setLoading(false)});
    return()=>ctrl.abort();
  },[selected,offset]);
  if(!datasets.length&&!error)return null;
  return <section className="dataset-browser"><b>可查询数据集</b><p className="edit-hint">完整数据用于筛选和计算，下方分块是检索目录。修改数据请编辑原文件后重新导入。</p>
    {datasets.map(d=><button className={`dataset-choice ${selected===d.id?'selected':''}`} key={d.id} onClick={()=>{setSelected(d.id);setOffset(0)}}><strong>{d.name}</strong><span>{d.row_count.toLocaleString()} 行 · {d.columns.length} 列 · v{d.revision}</span>{d.formula_count>0&&<small>公式 {d.formula_count} 个 · 缓存缺失 {d.missing_formula_cache} 个</small>}</button>)}
    {more&&<button disabled={catalogLoading} onClick={()=>setCatalogOffset(n=>n+50)}>{catalogLoading?'正在加载…':'更多数据集'}</button>}
    {error&&<p className="error-text">{error}</p>}{loading&&<p>正在读取数据…</p>}
    {page&&<><div className="dataset-preview"><table><thead><tr>{page.columns.map(c=><th key={c}>{c==='_source_row'?'原始行号':c}</th>)}</tr></thead><tbody>{page.rows.map((row,i)=><tr key={i}>{row.map((v,j)=><td key={j}>{v==null?'—':String(v)}</td>)}</tr>)}</tbody></table></div><div className="pagination"><button disabled={!offset} onClick={()=>setOffset(Math.max(0,offset-50))}>上一页</button><span>{offset+1}–{offset+page.rows.length} / {page.total}</span><button disabled={page.next_offset==null} onClick={()=>setOffset(page.next_offset)}>下一页</button></div></>}
  </section>;
}
