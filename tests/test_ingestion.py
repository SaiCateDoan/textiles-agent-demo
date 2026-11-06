import io
import zipfile

import pymupdf as fitz
import pytest
from PIL import Image

from backend.ingest import Block, clean, decode, extract, split_blocks, table_blocks


def test_chinese_normalize_encoding_and_overlap():
    assert decode('纺织工艺'.encode('gb18030'))=='纺织工艺'
    assert clean('ＡＢＣ\x00\t  参数\n\n\n说明')=='ABC 参数\n\n说明'
    original='工艺说明。'*300
    blocks=split_blocks([Block(original,'第1页')])
    assert len(blocks)>2 and all(len(b['text'])<=420 for b in blocks)
    assert blocks[0]['text'][-60:]==blocks[1]['text'][:60]


def test_csv_preserves_units_rows_and_chinese(sample_root):
    data=extract(sample_root/'01_MES生产日报_模拟.csv','.csv')
    assert len(data.blocks)==1 and data.datasets[0]['row_count']==12
    dataset=data.datasets[0]; names=[c['name'] for c in dataset['columns']]
    assert dataset['rows'][0][names.index('不良率_pct')+1]=='1.2'
    assert dataset['rows'][2][names.index('批次')+1]=='TX-202610-003'
    assert dataset['rows'][0][0]==2


def test_markdown_tables_keep_structured_data(tmp_path):
    p=tmp_path/'table.md';p.write_text('# 数据\n|温度_C|缩水率_pct|\n|---|---|\n|160|3.7|\n|170|2.7|',encoding='utf8')
    e=extract(p,'.md')
    tables=[b for b in e.blocks if b.kind=='document_table']
    assert len(tables)==1 and tables[0].fields['rows']==[['160','3.7'],['170','2.7']]


def test_html_strips_scripts_and_keeps_table(tmp_path):
    p=tmp_path/'test.html';p.write_text('<h1>工艺</h1><script>secret()</script><table><tr><th>温度_C</th><th>批次</th></tr><tr><td>165</td><td>TX003</td></tr></table>',encoding='utf8')
    e=extract(p,'.html')
    assert any(b.fields.get('rows')==[['165','TX003']] for b in e.blocks)
    assert 'secret' not in '\n'.join(b.text for b in e.blocks)


def test_office_parser_fixtures(tmp_path):
    # These are tiny programmatic format fixtures, not authored user documents.
    from docx import Document
    from openpyxl import Workbook
    from pptx import Presentation
    d=Document();d.add_paragraph('纺织工艺 TX-202610-003');t=d.add_table(rows=2,cols=2)
    for cell,text in zip([t.cell(0,0),t.cell(0,1),t.cell(1,0),t.cell(1,1)],['温度_C','缩水率_pct','165','3.2']):cell.text=text
    p=tmp_path/'test.docx';d.save(p)
    e=extract(p,'.docx');assert any(b.fields.get('rows')==[['165','3.2']] for b in e.blocks)
    wb=Workbook();ws=wb.active;ws.title='测试';ws.append(['机台','不良率_pct']);ws.append(['定型A',1.2]);p=tmp_path/'test.xlsx';wb.save(p)
    e=extract(p,'.xlsx');assert e.datasets[0]['rows'][0][1]=='定型A' and e.warnings
    ppt=Presentation();slide=ppt.slides.add_slide(ppt.slide_layouts[1]);slide.shapes.title.text='纯棉平纹工艺';p=tmp_path/'test.pptx';ppt.save(p)
    assert '纯棉' in extract(p,'.pptx').blocks[0].text


def test_text_pdf_chinese(sample_root):
    e=extract(sample_root/'08_坯布参数_模拟.pdf','.pdf')
    assert e.ocr_pages==0
    assert '145' in ''.join(b.text for b in e.blocks) and '坯布' in e.blocks[0].text


def test_real_chinese_ocr_on_scan_pdf_and_mixed_pdf(sample_root):
    p=sample_root/'06_纯扫描质检报告_模拟.pdf'
    with fitz.open(p) as pdf:
        assert not pdf[0].get_text().strip(), 'Fixture must be raster-only'
    e=extract(p,'.pdf')
    text=''.join(b.text for b in e.blocks)
    assert e.ocr_pages==1 and '146' in text and 'TX-202610-003' in text and '4.2' in text
    e=extract(sample_root/'07_含图工艺附件_模拟.pdf','.pdf')
    assert any(b.kind=='text' for b in e.blocks) and any(b.kind=='ocr' for b in e.blocks)
    assert '146' in ''.join(b.text for b in e.blocks if b.kind=='ocr')


def test_multiframe_tiff_and_blank_ocr(tmp_path):
    import backend.ingest as module
    img=Image.new('RGB',(300,150),'white');p=tmp_path/'blank.png';img.save(p)
    with pytest.raises(ValueError,match='未提取'):
        extract(p,'.png')


def test_table_duplicate_headers_and_empty_rows():
    b=table_blocks([['值','值'],['1','2'],['','']], '表格')
    assert len(b)==1 and b[0].fields['headers']==['值','值_2'] and b[0].fields['rows']==[['1','2']]
    physical=table_blocks([[],['温度_C'],[],['165']], '表格')
    assert physical[0].fields['source_rows']==[4]


def test_corrupt_pdf_rejected(tmp_path):
    p=tmp_path/'bad.pdf';p.write_bytes(b'not a PDF')
    with pytest.raises(Exception):extract(p,'.pdf')


def test_rotated_pdf_has_coherent_list_and_no_unnecessary_ocr(tmp_path):
    from backend.pdf_layout import page_text
    path=tmp_path/'synthetic_rotated_checklist.pdf'
    expected=['Keep batch labels readable','Record bath temperature before loading','Use clean water for each trial',
              'Check the sample width','Record the machine identifier','Separate trial and production records',
              'Label retained samples','Review the measured shrinkage','Archive the inspection sheet']
    with fitz.open() as pdf:
        for number in range(3):
            page=pdf.new_page(width=800,height=600)
            if number<2:page.insert_text((72,72),f'Synthetic context page {number+1}; no scanned content.')
        page=pdf[2]
        page.insert_text((50,550),'Synthetic dyeing checklist',rotate=90)
        for line,text in enumerate(expected):
            offset=0
            # Independent word spans reproduce raw-coordinate sorting failures.
            for word in text.split():
                page.insert_text((100+line*35,550-offset),word,rotate=90)
                offset+=fitz.get_text_length(word+' ',fontsize=11)
        page.set_rotation(90)
        pdf.save(path)
    with fitz.open(path) as pdf:
        assert pdf[2].rotation==90
        text=page_text(pdf[2])
        assert text.startswith('Synthetic dyeing checklist')
        positions=[text.index(phrase) for phrase in expected]
        assert positions==sorted(positions)
    data=extract(path,'.pdf')
    assert data.pages==3 and data.ocr_pages==0
    third=next(b for b in data.blocks if b.locator=='第 3 页')
    assert all(phrase in third.text for phrase in expected)


@pytest.mark.parametrize('rotation',[0,90,180,270])
def test_layout_normalizes_all_page_rotations(rotation):
    from backend.pdf_layout import page_text
    with fitz.open() as pdf:
        page=pdf.new_page(width=800,height=800)
        page.insert_text((400,400),'Should have excellent stability',rotate=rotation)
        page.set_rotation(rotation)
        assert page_text(page)=='Should have excellent stability'


def test_english_chunks_do_not_start_or_end_with_half_a_word():
    words=[f'fiber{i:03d}' for i in range(150)]
    chunks=split_blocks([Block(' '.join(words),'正文')])
    extracted={word for c in chunks for word in c['text'].split()}
    assert extracted==set(words)
