# 织知 Textile Agent

面向纺织工艺、生产与质检资料的 Agentic RAG 工作台。支持普通文档入库、可选多模态入库、带来源的知识问答，以及基于结构化数据的图表和交互工况控件。

项目采用前后端分离：React/Vite 提供界面，FastAPI 提供接口，LangGraph 编排 DeepSeek Responses API 的工具调用；原件、分块、图片、会话和向量保存在本机。当前开发与验收环境为 Windows + PowerShell。

## 功能

- **Agent 对话**：模型可以查看知识库、改写查询、检索、分页读取原文，再根据证据回答。结果带来源位置、文档版本和工具执行记录。
- **边回复边执行**：公开文字实时显示，阶段性回复后可以继续检索、计算、生成图表和滑块；历史按顺序保留文字与工具，停止或报错时保留已有内容。
- **知识库管理**：批量上传、查看解析/索引状态、检查和编辑分块、核对 PDF 原页、重新解析及移除文档。
- **可恢复入库**：持久任务队列、固定工作数、解析超时隔离、视觉限流和成功结果缓存；重新解析失败仍可检索已发布版本。
- **普通入库**：处理文本、Office、表格、PDF 与图片 OCR，保留页码、工作表和物理行号。
- **可选多模态入库**：DeepSeek 判断图片应转成文字还是保留原图；原图挂载到具体文段，一个分块可以关联多张图片。无文字独立图片生成描述并附图；无图文件自动走普通流程。
- **图表与控件**：Agent 可从文章、表格或转写资料中整理图表，支持饼图、散点、多系列、多坐标轴等，不绑定原始列名；图表保留来源。工况滑块按历史观测计算，不执行模型生成的代码。

详细实现与取舍见 [关键设计说明](docs/design.md)。多模态的使用方式、接口字段和格式边界见 [多模态入库说明](docs/multimodal.md)。

## 快速启动

### 环境要求

| 项目 | 要求 |
|---|---|
| Python | 3.11 或 3.12，虚拟环境与依赖由 uv 管理 |
| Node.js | 满足 Vite 的 `^20.19.0 \|\| >=22.12.0`；本机使用 Node 24 |
| 包管理 | uv、npm；仓库包含 `uv.lock` 和 `frontend/package-lock.json` |
| 模型服务 | 后端环境中的 `DEEPSEEK_API_KEY`；对话/视觉调用需要联网及可用额度 |
| 本地推理 | BGE 中文嵌入 + RapidOCR，默认 ONNX CPU，无需安装 Ollama、PyTorch 或 CUDA |

### 1. 安装项目依赖

在项目根目录执行：

```powershell
$env:UV_CACHE_DIR = "$PWD\.cache\uv"
uv sync --frozen --python 3.11
npm.cmd --prefix frontend ci
```

uv 会创建项目内的 `.venv`。首次使用嵌入模型会下载模型文件，缓存到 `data/models/`；首次安装和下载需要网络。

### 2. 配置模型

后端读取系统环境变量，也读取根目录 `.env`。如果系统已经配置 `DEEPSEEK_API_KEY`，可以直接使用；需要 `.env` 时，可先创建模板：

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

自行编辑 `.env` 中的密钥；不要把真实密钥写进前端、源码、截图或 Git。环境变量优先于 `.env`，修改配置后需重启后端。

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 空 | DeepSeek 密钥，仅由后端读取 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容接口地址 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 对话及工具调用模型 |
| `DEEPSEEK_VISION_MODEL` | 空 | 留空沿用对话模型；设置时必须支持图像输入 |
| `CHAT_MAX_ROUNDS` | `8` | 单轮任务最多请求模型的次数，最后一次不再提供工具 |
| `CHAT_TIMEOUT_SECONDS` | `300` | 包含文字和工具步骤的整轮对话超时 |
| `TEXTILE_DATA_DIR` | 项目下 `data/` | 原件、数据库、图片、模型与运行数据目录 |
| `EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | 中文 512 维嵌入模型 |
| `ONNX_PROVIDER` | `CPUExecutionProvider` | 嵌入执行提供者；默认依赖为 CPU 版 |
| `MAX_MULTIMODAL_IMAGES` | `60` | 单文档多模态图片数量上限 |
| `MAX_UPLOAD_MB` | `30` | 单文件上传大小上限 |

当前 LanceDB 表固定为 512 维；更换其他维度的嵌入模型需要修改表结构并重建索引，不能只修改配置后继续复用原表。OCR 在当前实现中保持 CPU。

聊天和图片分析均使用 DeepSeek Responses API。其接口无服务端会话存储，后端负责回传上下文和配对工具结果，不依赖 `previous_response_id`。详见 [Responses 与持续工具调用设计](docs/design.md#8-responses-api-与回复后继续执行工具) 和 [DeepSeek 官方指南](https://api-docs.deepseek.com/guides/responses_api/)。

并发与恢复默认配置为 2 个文档工作协程、1 万条任务容量、解析超时 300 秒、视觉同时 2 个请求/每分钟 30 次。可调整参数、独立 worker 启动方式及部署边界见 [设计说明第 7 节](docs/design.md#7-大积压与并发入库的实现)。不要仅靠增加工作数扩大吞吐，特别是 8GB 显存环境。

### 3. 启动前后端

分别在两个终端执行：

```powershell
# 终端一：在项目根目录
uv run --frozen uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

```powershell
# 终端二：在项目根目录
npm.cmd --prefix frontend run dev
```

也可以在依赖安装后执行 Windows 启动脚本：

```powershell
.\scripts\start.ps1
```

脚本以隐藏窗口启动服务，日志写入 `data/`。它不替换已经占用端口的进程，打印地址也不代表占用者一定是本项目；启动较慢时可检查健康接口和后端日志。使用独立终端启动时，按 `Ctrl+C` 可停止对应服务。

| 入口 | 地址 |
|---|---|
| 前端工作台 | [http://127.0.0.1:5173](http://127.0.0.1:5173) |
| 后端交互式 API 文档 | [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) |
| 后端健康状态 | [http://127.0.0.1:8000/api/health](http://127.0.0.1:8000/api/health) |

请通过 HTTP 地址访问前端，不要直接打开 `frontend/index.html`。Vite 将 `/api` 请求代理到 8000；后端允许本机 5173 的跨域请求。

## 开始使用

### 普通文档入库

1. 进入「知识库管理」，选择或拖入文件。
2. 在确认框中保持「启用多模态入库」关闭，点击「开始导入」。
3. 等待状态依次经过等待处理、解析/OCR、向量索引，最终变为「索引就绪」。上传返回成功仅表示文件已接收，处理仍可能失败。
4. 选择文档检查分块。PDF 可点击「核对原文页」；文字可编辑，保存会更新结构化字段、向量与版本。

表格编辑保留全部列名和单位，使用 `列名: 值; 列名: 值` 格式。带图分块必须保留完整图片标记。重新解析会从原件重建并覆盖手工校对，需要确认；新版本发布前继续查询旧版本，失败也保留旧版。历史回答保留当时的来源快照。

### 多模态入库

选择文件后勾选「启用多模态入库」。开关每批默认关闭，图片和局部邻文会发送到 DeepSeek，并产生额外视觉调用费用。

| 图片情况 | 入库结果 |
|---|---|
| 清晰文字截图、可完整转写的图形 | 标注为模型生成的摘要/转写，参与文本检索 |
| 布样、实物照片、需要保留外观的复杂图形 | 原文点位标记 + 中文视觉描述 + 对应原图 |
| 无可读文字的独立图片 | 独立视觉描述段 + 原图 |
| 文件没有图片 | 原普通解析流程，不调用视觉模型 |

图片归属到包含该点位的分块，检索和来源查看带回对应图片。多模态检索使用图像描述的文本向量，当前没有独立图像向量模型。详细规则与限制见 [多模态入库说明](docs/multimodal.md)。

### 对话、图表与工况探索

导入样本后，可以直接提问：

> 请按机台对比 MES 生产日报的平均不良率，生成柱状图，说明平均口径并引用来源。

> 根据定型工况实验，生成温度与缩水率趋势图，并创建温度滑块，查看相近历史工况建议。

> 查找 MM-DEMO-A/B 所在文段，比较两张织纹示意的可见颜色和图案，展示原图并引用来源。

`create_chart` 和 `create_scenario` 都是 Agent 工具。滑块生成后，拖动通过独立后端接口计算，无需每次询问大模型。历史相近工况均值不代表因果预测、生产配方或最优工艺；新版本发布后旧控件会拒绝继续计算，需要重新生成。

图表只使用一个宽松接口：模型提交图表配置和已查阅的来源文段，可直接提取文章内部数据，自选系列、坐标或无坐标轴图形。它不再自动按完整表格聚合；提取与计算口径由模型在说明中交代，来源存在不等于每个数据点已逐项核验。可导入 [纯文章图表样本](samples/10_文章图表数据_模拟.txt)，要求生成纤维占比环形图和温度—缩水率散点图。

## 技术栈与代码导航

| 层 | 实现 | 主要入口 |
|---|---|---|
| 界面 | React 19、Vite、ECharts、滑块、Markdown | [DataChart.jsx](frontend/src/DataChart.jsx)、[main.jsx](frontend/src/main.jsx)、[Passage.jsx](frontend/src/Passage.jsx) |
| 接口与任务 | FastAPI、异步导入任务、SSE | [main.py](backend/main.py) |
| 并发与恢复 | SQLite 租约队列、隔离解析、检查点、内容缓存 | [pipeline.py](backend/pipeline.py)、[jobs.py](backend/jobs.py)、[worker.py](backend/worker.py) |
| Agent 编排 | LangGraph、DeepSeek 工具调用循环 | [agent.py](backend/agent.py) |
| 前置清洗 | 多格式解析、PDF 显示坐标、RapidOCR | [ingest.py](backend/ingest.py)、[pdf_layout.py](backend/pdf_layout.py) |
| 多模态入库 | 图片定位、视觉判断、原子点位、图文关联 | [multimodal.py](backend/multimodal.py)、[media.py](backend/media.py) |
| 检索与计算 | BGE、jieba/BM25、RRF、图表配置及来源、近邻工况 | [retrieval.py](backend/retrieval.py)、[charts.py](backend/charts.py) |
| 数据持久化 | SQLite 元数据/会话；LanceDB 派生向量 | [store.py](backend/store.py) |

目录约定：

```text
backend/             Python 后端与 Agent
frontend/            独立前端、npm 锁文件
docs/design.md       清洗、入库、状态图和工具的设计说明
docs/multimodal.md   多模态使用及接口说明
samples/             公开资料与标注清楚的模拟测试原件
scripts/             启动、样本生成、模型检查和真实接口验收
tests/               自动测试及预期数值
artifacts/           人工验收记录；机器报告和预览在本机生成
data/                本机运行数据，不纳入 Git
.env.example         不含密钥的配置模板
pyproject.toml       Python 依赖声明
uv.lock              Python 依赖锁文件
```

## 支持格式与当前边界

普通入库支持 TXT、Markdown、CSV、TSV、XLSX、XLS、DOCX、PPTX、HTML、PDF、PNG、JPEG、WebP、BMP、TIFF。PDF 包括文字层、纯扫描及含图页面。旧 DOC/PPT 请转换为 DOCX/PPTX。

多模态支持 PDF 栅格图/可检测的矢量区域、DOCX 内嵌图、PPTX 图片形状、XLSX 锚定图、HTML/Markdown 内嵌 base64 图片，以及上述独立图片格式。HTML/Markdown 外链或未上传的相对路径图片不会自动读取或下载；旧 XLS 的多模态图片应转换为 XLSX。

默认限制：单文件 30MB、PDF/图片最多 200 页或帧、单表 2 万行、单文档 3 万块、Office 解压后 200MB。多模态最多 60 张图、单图 4000 万像素、整份标准化图片最多 100MB，视觉分析并发为 2。复杂多栏、合并表头、手写、精细图表数据和模型描述均需核对原件。

扫描/OCR 和视觉转写不会自动变成可信的结构化数值表；精确图表统计应使用原始表格或人工校对的数据。**索引就绪表示可检索，不表示原文已经校验正确。**

## 测试与演示材料

样本说明见 [普通样本](samples/README.md) 和 [多模态样本](samples/multimodal/README.md)。工艺单、MES、工况实验和报告中的模拟数字均不能当作真实生产记录；NPTEL PDF 是可自行获取的外部课件，不随仓库分发。回归测试使用项目生成的旋转 PDF，不需要该外部课件。

```powershell
# 自动测试：临时数据与当前 data 隔离，不调用真实 DeepSeek
uv run --frozen pytest -q --junitxml=artifacts/pytest.xml

# 构建前端
npm.cmd --prefix frontend run build

# 前端流式状态、中文分段与回复/工具渲染顺序测试
npm.cmd --prefix frontend test

# 首次下载中文嵌入并验证模型连通性：会调用真实 DeepSeek
uv run --frozen python scripts/check_services.py

# 服务启动后运行真实普通入库、RAG、图表与控件验收
uv run --frozen python scripts/acceptance.py

# 真实多模态入库、原图像素与带图对话验收
uv run --frozen python scripts/acceptance_multimodal.py

# 真实 Responses、图片分析、边回复边检索、图表及滑块；使用独立测试数据目录
uv run --frozen python scripts/acceptance_responses.py

# 真实模型从纯文章生成无轴图和散点图，并离线验证实际图表渲染
uv run --frozen python scripts/acceptance_charts.py

# 并发入库冒烟：真实解析与嵌入，不调用聊天/视觉；成功后删除自己的模拟文档
uv run --frozen python scripts/benchmark_ingestion.py --documents 40 --concurrency 12
```

真实验收会新增演示文档/对话并产生 API 费用。样本生成脚本使用 Windows 字体；普通样本可用 `scripts/make_samples.py` 重新生成，多模态验收脚本也会生成其测试原件。

并发改造前记录：普通真实接口 15 项、多模态真实接口 13 项检查通过。随后完成真实解析/BGE/LanceDB 的并发 HTTP 导入，以及真实 DeepSeek Responses 的带图检索、阶段性回复后继续工具、图表及滑块验收。当前自动测试、前端测试和构建结果见 [Responses 验收记录](artifacts/Responses与持续工具调用验收记录.md)。自动测试部分模型使用替身；上述结果不代替浏览器视觉验收，也不是十万份复杂文档的吞吐承诺。

已保留的人工记录：[项目验收记录](artifacts/验收记录.md)、[PDF 解析问题与修复](artifacts/PDF解析问题与修复.md)、[多模态验收记录](artifacts/多模态入库验收记录.md)。机器生成的 JSON/XML 报告和预览不进入 Git，运行相应脚本后在 `artifacts/` 查看。

## 数据与版本管理

本仓库包含源码、锁文件、文档、测试和测试原件。`.env`、运行数据库、上传文件、图片资产、模型缓存、虚拟环境、`node_modules`、构建结果、日志和机器验收报告被 `.gitignore` 排除；`.env.example` 可以提交。

密钥只在后端使用。普通入库的清洗/OCR/嵌入在本机运行；对话会将最近消息和检索证据发送给 DeepSeek；开启多模态后还会发送文档图片和局部邻文。当前服务仅监听本机回环地址，尚未实现账户、多租户隔离、生产鉴权或公开部署配置。

## 开源协议

项目原创代码、文档及模拟样本采用 [MIT License](LICENSE)，Copyright (c) 2026 SaiCateDoan。第三方依赖、模型权重和外部资料保留各自的许可与版权；MIT 协议不覆盖它们。NPTEL 课件原件仅保留为可选本地输入，不进入公开仓库或公开提交历史。
