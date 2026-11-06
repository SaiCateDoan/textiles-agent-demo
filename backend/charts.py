"""One declarative chart tool for prose, OCR, tables and multiple documents."""
import json
import math


def normalize_option(option):
    if not isinstance(option,dict):
        raise ValueError("option 必须是图表配置对象")
    count=0
    def walk(value,path=()):
        nonlocal count
        count+=1
        if count>100000 or len(path)>32:
            raise ValueError("图表配置过大，请减少数据点或嵌套层次")
        if isinstance(value,dict):
            result={}
            for key,item in value.items():
                if not isinstance(key,str) or key in {"__proto__","constructor","prototype"}:
                    raise ValueError("图表包含无效配置键")
                # Charts carry data, not links, image fetches or executable UI.
                if key in {"link","sublink","image","dataView"}:
                    continue
                if key=="reg" and "transform" in path:
                    raise ValueError("数据筛选请使用比较条件，不支持动态正则")
                result[key]=walk(item,(*path,key))
            context=next((p for p in reversed(path) if not p.isdigit()),None)
            if context=="tooltip":
                result.update(renderMode="richText",confine=True)
                result.pop("extraCssText",None)
            if context=="saveAsImage":
                result.update(name="textile-chart",type="png")
            return result
        if isinstance(value,list):
            return [walk(item,(*path,str(i))) for i,item in enumerate(value)]
        if isinstance(value,str):
            if len(value)>100000 or value.lower().startswith("image://"):
                raise ValueError("请使用内置图形符号，不载入外部图片")
            return value
        if value is None or isinstance(value,bool):
            return value
        if isinstance(value,(int,float)):
            try:
                if math.isfinite(value):return value
            except OverflowError:
                pass
        raise ValueError("图表只能包含有效JSON数据；数值必须有限，缺失值使用null")
    cleaned=walk(option)
    series=cleaned.get("series")
    series=[series] if isinstance(series,dict) else series
    if not isinstance(series,list) or not series or not all(isinstance(s,dict) and isinstance(s.get("type"),str) and s["type"] for s in series):
        raise ValueError("请提供至少一个带type的series；按数据选择图形，不强制坐标轴")
    if len(json.dumps(cleaned,ensure_ascii=False).encode())>1024*1024:
        raise ValueError("图表配置超过1MB，请减少数据点")
    if "tooltip" not in cleaned:
        cleaned["tooltip"]={"renderMode":"richText","confine":True}
    return cleaned


def chart(store, option, source_ids, title=None, description="", *, evidence_sources=None, document_ids=None):
    if not isinstance(source_ids,list) or not source_ids or len(source_ids)>100 or not all(isinstance(i,str) for i in source_ids):
        raise ValueError("请提供1至100个实际来源文段ID")
    ids=list(dict.fromkeys(source_ids))
    if evidence_sources is not None and any(i not in evidence_sources for i in ids):
        raise ValueError("图表来源尚未查阅，请先检索或读取相应文段，再使用其ID生成图表")
    rows=store.chunks_by_ids(ids)
    by_id={r["id"]:r for r in rows}
    if any(i not in by_id for i in ids):
        raise ValueError("部分来源不存在或已不可检索，请重新检索")
    rows=[by_id[i] for i in ids]
    if any(r["kind"]=="dataset" for r in rows):
        raise ValueError("数据集目录不是完整统计证据，请先query_datasets并使用create_data_chart")
    for row in rows:
        if document_ids is not None and row["document_id"] not in document_ids:
            raise ValueError("图表来源不在当前检索范围内")
        if evidence_sources is not None:
            previous=evidence_sources[row["id"]]
            if previous["revision"]!=row["revision"] or previous["text"]!=row["text"]:
                raise ValueError("图表来源已变化，请重新检索后生成")
    cleaned=normalize_option(option)
    if title is not None and (not isinstance(title,str) or len(title)>200):
        raise ValueError("标题应为不超过200字的文本")
    if not isinstance(description,str) or len(description)>3000:
        raise ValueError("说明应为不超过3000字的文本")
    option_title=cleaned.get("title",{})
    if isinstance(option_title,list):option_title=next((t for t in option_title if isinstance(t,dict)),{})
    title=title or (option_title.get("text") if isinstance(option_title,dict) else None) or "数据图表"
    documents=list({r["document_id"]:{"id":r["document_id"],"name":r["document_name"],"revision":r["revision"]} for r in rows}.values())
    return dict(type="chart",title=str(title),description=description,option=cleaned,source_ids=ids,
                documents=documents,document_id=documents[0]["id"] if len(documents)==1 else None,
                revision=documents[0]["revision"] if len(documents)==1 else None,
                data_origin="model_organized",_source_rows=rows)
