# 多模态入库测试材料

本目录图片由程序绘制，用于验证纺织图片入库，不是真实织物照片。报告数字和文档内容均为软件测试模拟数据。

- `01_blue_herringbone_synthetic.png`：无文字的蓝灰人字纹示意，可单独上传并勾选多模态，验证“描述+原图”。
- `02_orange_check_synthetic.png`：无文字的橙褐格纹示意。
- `03_readable_report_synthetic.png`：清晰文字截图，验证模型是否将可读内容转为文字。
- `04_multimodal_demo_synthetic.docx`：同一段插入两张纹理图，另一段插入报告截图，末尾有无图段落。勾选多模态后，应看到两图只属于素材比较段、报告转写保留145 g/m2和批次MM-TEXT-001，无图段落不附图。
- `05_plain_fallback.txt`：开启多模态仍直接使用普通文本入库，不产生视觉请求。

重新生成与自动验收：`uv run python scripts/acceptance_multimodal.py`。该命令会实际调用DeepSeek并新增文档和对话。
