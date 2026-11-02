import pytest
from backend.retrieval import VectorIndex, chart, scenario, evaluate, number
from conftest import load_doc


@pytest.mark.parametrize('value,expected',[('1,200',1200),('2.1%',2.1),('0',0),('-1.5',-1.5),('2026-10-20',None),('3~5',None),('<=3',None),('3 kg',None),('NaN',None)])
def test_numbers_do_not_confuse_units_dates_and_ranges(value,expected):
    assert number(value)==expected


def test_whole_table_aggregate_exact(storage,sample_root):
    doc=load_doc(storage,sample_root/'01_MES生产日报_模拟.csv');s,idx=storage
    a=chart(s,doc,'机台','不良率_pct')
    assert {p['name']:p['value'] for p in a['data']}=={'定型A':1.25,'定型B':2.1,'定型C':.85}
    assert len(a['source_ids'])==12 and a['skipped_rows']==0
    assert all(p['count']==4 for p in a['data'])
    with pytest.raises(ValueError,match='列名不存在'):chart(s,doc,'机台','不良率')
    with pytest.raises(ValueError):chart(s,doc,'机台','不良率_pct','execute_python')


def test_scenario_range_neighbours_and_reference_change(storage,sample_root):
    doc=load_doc(storage,sample_root/'02_定型工况实验_模拟.csv');s,_=storage
    a=scenario(s,doc,'定型温度_C','缩水率_pct')
    assert (a['min'],a['max'])==(150,185) and len(a['observed'])==15
    low=evaluate(s,doc,'定型温度_C','缩水率_pct',150)
    high=evaluate(s,doc,'定型温度_C','缩水率_pct',180)
    assert low['reference']==5.1 and high['reference']==2.45
    assert low['source_ids']!=high['source_ids']
    with pytest.raises(ValueError,match='观测范围'):evaluate(s,doc,'定型温度_C','缩水率_pct',200)
    with pytest.raises(ValueError):evaluate(s,doc,'定型温度_C','缩水率_pct',float('nan'))
    with pytest.raises(ValueError):scenario(s,doc,'车速_m_min','缩水率_pct')


def test_lancedb_persistence_scope_and_deleted_metadata(storage,sample_root):
    s,idx=storage
    d1=load_doc(storage,sample_root/'01_MES生产日报_模拟.csv')
    d2=load_doc(storage,sample_root/'03_染整工艺单_模拟.md')
    hits=idx.search(s,'TX-202610-003 经向缩水 待复检',[d2])
    assert hits and all(h['document_id']==d2 for h in hits)
    assert idx.db.open_table('chunks').count_rows()>12
    s.execute('DELETE FROM documents WHERE id=?',(d2,))
    # Metadata remains authoritative even if a vector delete was interrupted.
    assert not idx.search(s,'经向缩水',[d2])
    idx.delete(d2)
    assert idx.table.count_rows()==12
    assert not s.chunks(d2)


def test_stale_revisions_never_used(storage,sample_root):
    s,idx=storage;doc=load_doc(storage,sample_root/'01_MES生产日报_模拟.csv')
    original=s.chunks(doc)
    for c in original:c['revision']=2
    s.replace_chunks(doc,original)
    # Vector scores from old revision are ignored; lexical results use current authoritative text.
    hits=idx.search(s,'定型A 不良率')
    assert hits and all(h['revision']==2 for h in hits)


def test_chart_source_snapshot_matches_calculation_during_publish(storage,sample_root):
    s,idx=storage;doc=load_doc(storage,sample_root/'01_MES生产日报_模拟.csv')
    result=chart(s,doc,'机台','不良率_pct')
    originals=result['_source_rows']
    changed=s.chunks(doc)
    for c in changed:
        c['revision']=2
        c['text']='新版本数据'
    s.replace_chunks(doc,changed)
    assert result['revision']==1
    assert all(r['revision']==1 and r['text']!='新版本数据' for r in originals)
