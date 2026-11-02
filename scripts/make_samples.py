"""Reproducible synthetic fixtures. No real factory/customer information."""
import csv
import io
import json
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples"
OUT.mkdir(exist_ok=True)
(ROOT / "artifacts").mkdir(exist_ok=True)
(ROOT / "tests").mkdir(exist_ok=True)


def write_csv(name, headers, rows):
    with (OUT/name).open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


def main():
    mes_headers = ["数据性质", "日期", "批次", "机台", "品种", "产量_m", "不良率_pct", "能耗_kwh", "停机_min"]
    mes_rows = []
    for i, (machine, defects, energy, output) in enumerate([
        ("定型A",1.2,420,5200),("定型A",1.4,430,5300),("定型A",1.1,415,5150),("定型A",1.3,425,5250),
        ("定型B",2.1,480,5000),("定型B",2.3,490,5100),("定型B",1.9,470,4950),("定型B",2.1,480,5050),
        ("定型C",.8,390,5400),("定型C",1,400,5500),("定型C",.7,385,5350),("定型C",.9,395,5450),
    ], 1):
        mes_rows.append(["模拟数据",f"2026-10-{20+(i-1)//3:02d}",f"TX-202610-{i:03d}",machine,"C32S纯棉平纹",output,defects,energy,10+i])
    write_csv("01_MES生产日报_模拟.csv",mes_headers,mes_rows)
    exp_headers=["数据性质","实验编号","品种","定型温度_C","车速_m_min","缩水率_pct","克重_g_m2","能耗_kwh"]
    exp_rows=[]
    for i,(temp,shrink,energy) in enumerate([(150,5.2,280),(150,5,285),(155,4.6,300),(155,4.4,305),(160,3.7,325),(160,3.9,330),(165,3.2,350),(165,3.3,355),(170,2.7,380),(170,2.9,385),(175,2.5,415),(175,2.6,420),(180,2.4,455),(180,2.5,460),(185,2.8,500)],1):
        exp_rows.append(["模拟数据",f"EXP-{i:03d}","C32S纯棉平纹",temp,25,shrink,145+(i%3),energy])
    write_csv("02_定型工况实验_模拟.csv",exp_headers,exp_rows)
    process="""# C32S纯棉平纹染整工艺单 模拟数据

本文件由项目人工编造，仅用于软件导入和检索验收，不是实际生产配方。编制日期：2026-10-25。批次：TX-202610-003。

## 坯布参数
成分为100%棉；纱支为32S；织物组织为平纹；目标克重145 g/m2；目标门幅150 cm。实际检测应以签字报告为准。

## 工艺路线与内部控制
验坯、退浆、煮练、漂白、染色、皂洗、中和、柔软、定型、预缩、检验。前处理不充分可能影响后续匀染。皂洗和充分水洗有助于去除未固着染料。
本模拟工艺以60 C染色、保温45 min作为内部演示设定。该设定是人工编造，不是引用公共资料的生产参数。
定型工况实验固定车速25 m/min，温度区间150至185 C。不同温度下的缩水率及能耗见02_定型工况实验_模拟.csv。实验只展示历史相关性，不能推断改变温度必然导致某结果。

## 批次 TX-202610-003 异常处置
模拟质检发现经向缩水率4.2%，超过内部演示限值3.0%；克重146 g/m2满足目标145±5 g/m2；门幅150 cm满足目标150±2 cm。结论为待复检，不直接放行。
建议核查预缩机张力、回潮与定型记录；查明实验条件后再做小样验证。上述建议是诊断方向，不是已确认的根因。

## 口径
不良率_pct是各批次的百分数。按机台平均指批次不良率算术平均，不能称为按产量加权的总体不良率。需要总体不良率时必须补充逐批不良数量。
"""
    (OUT/"03_染整工艺单_模拟.md").write_text(process,encoding="utf-8")
    (OUT/"04_前处理知识_公开来源摘要.txt").write_text("""公开资料摘要 非工厂工艺配方
来源：Cotton Incorporated / CottonWorks，Dyeing Preparation
链接：https://cottonworks.com/learning-hub/dyeing/dyeing-preparation/
访问日期：2026-11-02
以下为项目自行概括，非逐字摘录：棉织物染整前的准备质量会影响后续染整稳定性。煮练用于清除杂质、改善吸水性；漂白用于获得较均匀的白度。各工序要控制条件并检查残余化学品，记录变化以便追溯。具体处方须结合设备、基材和验证结果制定。
""",encoding="utf-8")
    # A real raster-only Chinese report: no hidden text layer in the scan PDF.
    font_path=Path("C:/Windows/Fonts/msyh.ttc")
    if not font_path.exists():
        font_path=Path("C:/Windows/Fonts/simhei.ttf")
    title_font=ImageFont.truetype(str(font_path),52)
    font=ImageFont.truetype(str(font_path),31)
    small=ImageFont.truetype(str(font_path),25)
    img=Image.new("RGB",(1654,1900),"white")
    d=ImageDraw.Draw(img)
    d.text((100,95),"纺织面料质量检验报告",font=title_font,fill="#202d28")
    d.text((100,190),"模拟数据  仅用于软件验收  非实际质检凭证",font=font,fill="#875a45")
    lines=["报告编号: QC-DEMO-20261025", "批次: TX-202610-003", "面料: C32S 纯棉平纹  成分: 100%棉", "检验日期: 2026-10-25", "检验项目                 实测值                  内部演示要求", "克重                     146 g/m2               145 +/- 5 g/m2", "门幅                     150 cm                 150 +/- 2 cm", "经向缩水率               4.2%                   不大于 3.0%", "纬向缩水率               2.1%                   不大于 3.0%", "结论: 经向缩水率超出演示限值，待复检。", "建议: 核查预缩张力、回潮及定型记录。", "注意: 原始扫描识别结果需人工校对数字和单位。"]
    for i,line in enumerate(lines):
        y=295+i*98
        d.text((100,y),line,font=font,fill="#273c32")
        if 4<=i<=8:
            d.line((95,y+64,1550,y+64),fill="#bbc5b9",width=2)
    d.text((100,1680),"Synthetic textile quality report for ingestion and OCR testing.",font=small,fill="#7b8978")
    img.save(OUT/"05_扫描质检报告_模拟.png")
    buf=io.BytesIO();img.save(buf,format="PNG")
    c=canvas.Canvas(str(OUT/"06_纯扫描质检报告_模拟.pdf"),pagesize=(595,684))
    c.drawImage(ImageReader(io.BytesIO(buf.getvalue())),0,0,width=595,height=684);c.save()
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    c=canvas.Canvas(str(OUT/"07_含图工艺附件_模拟.pdf"),pagesize=(595,842))
    c.setFont("STSong-Light",18);c.drawString(45,795,"工艺复核附件 模拟数据")
    c.setFont("STSong-Light",11)
    for j,line in enumerate(["批次 TX-202610-003 的正文层可直接提取，以下报告图片需 OCR。", "本附件用于验证含文字层且有内嵌扫描图的 PDF，不是生产依据。", "图片内含检验结果，工艺建议需与签字原件及实际工况共同验证。"]):
        c.drawString(45,765-j*22,line)
    c.drawImage(ImageReader(io.BytesIO(buf.getvalue())),70,75,width=455,height=523);c.save()
    c=canvas.Canvas(str(OUT/"08_坯布参数_模拟.pdf"),pagesize=(595,842))
    c.setFont("STSong-Light",19);c.drawString(50,780,"坯布参数卡 模拟数据")
    c.setFont("STSong-Light",13)
    for j,line in enumerate(["文件性质：项目软件验收模拟数据。", "品种：C32S 纯棉平纹；成分：100%棉。", "目标克重：145 g/m2；目标门幅：150 cm。", "批次：TX-202610-003；状态：待复检。", "所有数值为人工编造，不得作为真实生产规格。"]):
        c.drawString(50,720-j*42,line)
    c.save()
    # Visual QA images for all authored PDF pages.
    for file in sorted(OUT.glob("0[678]*.pdf")):
        with fitz.open(file) as pdf:
            for i,page in enumerate(pdf):
                page.get_pixmap(matrix=fitz.Matrix(1.25,1.25)).save(ROOT/"artifacts"/f"{file.stem}-page-{i+1}.png")
    expected={"mes_mean_defect_pct":{"定型A":1.25,"定型B":2.1,"定型C":.85},"mes_total_output_m":sum(r[5] for r in mes_rows),"experiment_rows":len(exp_rows),"scan_batch":"TX-202610-003","scan_weight":146,"scan_shrinkage_pct":4.2}
    (ROOT/"tests"/"expected.json").write_text(json.dumps(expected,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Generated reproducible sample documents and PDF QA renders.")


if __name__=="__main__":
    main()
