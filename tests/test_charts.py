import copy
import pytest

from backend.charts import chart,normalize_option
from conftest import load_doc


def article(storage,tmp_path,name='article.txt',text='模拟工艺文章：棉45%、涤纶35%、粘胶20%。温度150℃时缩水率5.1%，180℃时为2.45%。'):
    path=tmp_path/name;path.write_text(text,encoding='utf8')
    doc=load_doc(storage,path)
    return doc,storage[0].chunks(doc)[0]


def test_article_can_generate_axis_free_pie_without_table_fields(storage,tmp_path):
    doc,row=article(storage,tmp_path)
    option={'legend':{},'series':{'type':'pie','radius':['40%','70%'],'data':[{'name':'棉','value':45},{'name':'涤纶','value':35},{'name':'粘胶','value':20}]}}
    before=copy.deepcopy(option)
    result=chart(storage[0],option,[row['id']],title='模拟纤维配比',description='由文章明示百分比整理')
    assert result['option']['series']['type']=='pie'
    assert 'xAxis' not in result['option'] and 'yAxis' not in result['option']
    assert option==before and result['document_id']==doc
    assert result['_source_rows'][0]['kind']=='text'
    assert result['source_ids']==[row['id']]


def test_dataset_multiple_series_and_axes_are_preserved(storage,tmp_path):
    _,row=article(storage,tmp_path)
    option={'dataset':{'source':[['月份','产量','不良率'],['一月',100,2],['二月',120,1.8]]},
            'xAxis':{'type':'category'},'yAxis':[{'name':'产量_m'},{'name':'不良率_%','position':'right'}],
            'series':[{'type':'bar','encode':{'x':'月份','y':'产量'}},{'type':'line','yAxisIndex':1,'encode':{'x':'月份','y':'不良率'}}]}
    result=chart(storage[0],option,[row['id']])
    assert len(result['option']['series'])==len(result['option']['yAxis'])==2
    assert result['option']['dataset']==option['dataset']
    assert result['option']['series'][1]['yAxisIndex']==1


def test_chart_can_combine_multiple_documents_and_preserve_source_versions(storage,tmp_path):
    first,a=article(storage,tmp_path,'a.txt')
    second,b=article(storage,tmp_path,'b.txt','模拟设备说明：A机台120米/小时，B机台145米/小时。')
    result=chart(storage[0],{'series':[{'type':'scatter','data':[[150,5.1],[180,2.45]]}]},[a['id'],b['id'],a['id']])
    assert result['document_id'] is None and result['revision'] is None
    assert {d['id'] for d in result['documents']}=={first,second}
    assert result['source_ids']==[a['id'],b['id']]


def test_evidence_must_be_read_in_current_run_and_in_scope(storage,tmp_path):
    doc,row=article(storage,tmp_path)
    option={'series':[{'type':'bar','data':[45]}]}
    with pytest.raises(ValueError,match='尚未查阅'):
        chart(storage[0],option,[row['id']],evidence_sources={})
    with pytest.raises(ValueError,match='检索范围'):
        chart(storage[0],option,[row['id']],evidence_sources={row['id']:row},document_ids=['other'])
    assert chart(storage[0],option,[row['id']],evidence_sources={row['id']:row},document_ids=[doc])
    old=copy.deepcopy(row)
    changed=storage[0].chunks(doc)
    changed[0]['text']='新版本数值';changed[0]['revision']=2
    storage[0].replace_chunks(doc,changed)
    with pytest.raises(ValueError,match='已变化'):
        chart(storage[0],option,[row['id']],evidence_sources={row['id']:old})


@pytest.mark.parametrize('option',[{}, {'series':[]},{'series':[{'data':[1]}]}, {'series':[{'type':'bar','data':[float('nan')]}]}, {'series':[{'type':'bar','data':[float('inf')]}]}, {'series':[{'type':'bar','data':[10**400]}]}])
def test_chart_rejects_unrenderable_json_values(option):
    with pytest.raises(ValueError):normalize_option(option)


def test_option_cannot_load_external_images_execute_ui_or_use_unsafe_tooltips():
    option={'title':{'text':'示意','link':'javascript:alert(1)'},'tooltip':{'renderMode':'html','formatter':'{b}: {c}','extraCssText':'unsafe'},
            'toolbox':{'feature':{'dataView':{'title':'<img>'},'saveAsImage':{'name':'../file','type':'html'}}},
            'series':[{'type':'bar','data':[1,2]}]}
    safe=normalize_option(option)
    assert 'link' not in safe['title']
    assert safe['tooltip']=={'renderMode':'richText','formatter':'{b}: {c}','confine':True}
    assert 'dataView' not in safe['toolbox']['feature']
    assert safe['toolbox']['feature']['saveAsImage']=={'name':'textile-chart','type':'png'}
    with pytest.raises(ValueError):normalize_option({'series':[{'type':'scatter','symbol':'image://https://outside.test/private','data':[[1,2]]}]})
    with pytest.raises(ValueError):normalize_option({'dataset':{'transform':{'type':'filter','config':{'reg':'(a+)+$'}}},'series':[{'type':'bar'}]})


def test_chart_config_size_and_depth_are_bounded():
    option={'series':[{'type':'bar','data':[1]}]}
    target=option
    for _ in range(35):target['nested']={};target=target['nested']
    with pytest.raises(ValueError):normalize_option(option)
    with pytest.raises(ValueError):normalize_option({'series':[{'type':'bar','data':['x'*10000]*120}]})
