# 拓言 · 门店营销助手

面向门店导购的产品图文创作工具。按具体型号从 IMA 知识库读取原文与图片，结合目标人群、场景和发布平台生成文案，再检查事实依据与宣传表述。

本仓库为探索版 V2 的代码发布版，包含应用代码、提示词、离线测试和演示任务。不包含真实凭证、知识库正文、产品图片、运行日志或历史生成结果。

## 功能

- **减少填写**：支持快捷选择、领取预设任务和一句话需求；缺少关键条件时最多补问两轮。
- **分型号取材**：支持电脑及手机资料入口，避免将不同型号资料混入同一创作任务。可选型号取决于所连接知识库中的资料。
- **适配三平台**：生成小红书、微信公众号、朋友圈内容，分别约束结构、语气、配图数量和行动引导。
- **调整创作角度**：按受众与使用场景选择卖点，支持换个角度重新生成。
- **区分选题与事实**：联网搜索辅助寻找话题；产品参数、价格、赠品和服务承诺仍需来自确认资料。
- **保留证据**：模型选择原文片段编号，由程序复制引句；写前检查支持关系，写后进行规则与语义核验。
- **管理事实卡**：本地记录型号、来源指纹、适用条件、有效期和确认信息，检查卡片是否仍可使用。
- **制作真实产品封面**：完整等比保留原图，由 Pillow 排版标题和型号；封面标题可输入，最多 30 字。
- **统一手机图片入口**：选择资料原图或上传实拍，最多 6 张去重，批量确认后供正文与封面共用。
- **领取成品**：提供纯净文案 TXT、文案与原图 ZIP，核验 Markdown 单独导出；未通过核验时不提供成品领取。
- **限制旧结果误用**：型号、任务、图片确认或封面标题变化后，相关旧结果失效。
- **主动保存笔记**：由用户点击后创建 IMA 个人笔记，不自动发布到社交平台。

## 技术栈

Python 3.11+、Streamlit、LangChain、Pydantic、Pillow、IMA OpenAPI 与 Node.js。

模型通过兼容 OpenAI 协议的接口调用。默认配置为 DeepSeek；实际接口地址、模型 ID 和密钥需与服务商账户对应。

生成采用固定 Python 流程：策略 → 检索当前型号资料 → 原文事实提取 → 写前支持审查与选材 → 生成 → 规则与语义核验。检索由程序控制；不依赖 LangGraph 或向量数据库。

## 快速开始

### Windows

1. 安装 Python 3.11 或更新版本，并将 Python 加入 PATH。
2. 双击 `install.bat`，安装依赖；首次安装会从模板创建本地 `.env`。
3. 编辑 `.env`，填写模型凭证、IMA 凭证和已加入的知识库完整名称。
4. 双击 `start.bat`，访问 <http://127.0.0.1:8504/>。

也可以在项目目录使用 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# 编辑 .env 后启动
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8504 --browser.gatherUsageStats false
```

已有 `.env` 时跳过复制命令。端口被占用时，将 `--server.port` 改为其他可用端口。

### macOS / Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 后启动
.venv/bin/python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8504 --browser.gatherUsageStats false
```

封面需要中文字体。Windows 会尝试系统微软雅黑和黑体，Linux 会尝试 Noto CJK 等常见位置；也可通过环境变量 `COVER_FONT_PATH` 指向本机有使用权限的中文字体文件。

IMA 需要 Node.js。依赖清单包含备用运行时 `nodejs-wheel-binaries`，程序优先使用 PATH 中的 Node，也支持该 Python 包内的运行时。

## 配置

将 `.env.example` 复制为 `.env`，所有密钥字段均需自行填写。程序读取项目目录下的 `.env`，系统同名环境变量优先。云端部署时应使用平台的私密环境变量功能，不上传本地 `.env`。

| 变量 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 模型接口密钥；也可使用 `LLM_API_KEY`，前者优先 |
| `LLM_BASE_URL` | 兼容接口地址，默认 `https://api.deepseek.com` |
| `LLM_MODEL` | 服务商实际支持的模型 ID，模板默认 `deepseek-v4-pro` |
| `LLM_TIMEOUT` | 请求超时秒数，默认 90，限制为 10–180 |
| `IMA_OPENAPI_CLIENTID` | IMA Client ID |
| `IMA_OPENAPI_APIKEY` | IMA API Key |
| `IMA_KB_NAME` | 当前账号已加入的知识库完整名称，按名称精确匹配 |
| `IMA_SKILL_PATH` | IMA 脚本位置，默认 `vendor/ima-skill/ima_api.cjs` |
| `WEB_SEARCH_PROVIDER` | `auto`、`tavily`、`sogou` 或 `bing` |
| `TAVILY_API_KEY` | 可选的 Tavily 搜索密钥 |
| `COVER_FONT_PATH` | 可选的中文字体文件路径，通过系统环境变量设置 |

IMA 两项凭证未填写时，官方脚本也支持读取本机 `~/.config/ima/client_id` 与 `~/.config/ima/api_key`。这些文件不属于仓库。

`auto` 优先使用已配置的 Tavily，否则尝试搜狗、Bing 公共搜索；公共通道可能限流或失效，失败时可改用本地选题。正式使用应选择企业认可的搜索服务。

连接自有知识库时，界面可能提示与原赛题指定库不同。这是参赛来源提示，不代表连接失败；参赛适用性需另行确认。

## 使用流程

1. 在侧栏检查模型与 IMA 配置，连接知识库。
2. 按具体型号加载产品原文及图片。
3. 快捷选择任务、领取预设任务，或输入一句话需求。
4. 确认产品图片的型号和使用权限；手机可在同一入口选择原图或上传实拍。
5. 生成图文，查看事实来源、正文与核验结果；必要时换角度生成。
6. 编辑封面标题并制作封面，核验通过后下载文案和图片包。

发布前仍需人工确认真实销售配置、有效期、服务政策及图片权限。系统不自动发布，也不会自动确认上传照片中的产品型号。

## 检查与离线演示

以下命令在项目目录运行，使用已安装项目依赖的 Python：

```bash
python -X utf8 tools/check_setup.py
python -m pytest -q
python -X utf8 tools/run_demo.py
```

默认诊断不联网；离线测试使用模拟资料，演示结果明确标记为模拟。它们不能代替真实模型质量验收。

需要自行确认真实连接时可运行：

```bash
python -X utf8 tools/check_setup.py --live-ima
python -X utf8 tools/check_setup.py --live-model
```

`--live-ima` 读取有权访问的知识库，`--live-model` 会发送真实模型请求并产生接口费用。

## 目录

```text
app.py                     Streamlit 应用入口
creation_ui.py             创作、图片与事实卡交互
workflow.py                生成和核验流程
config.py                  环境变量与本地配置
ima_client.py / ima_ui.py   知识库读取与个人笔记
llm.py / schemas.py         模型适配与结构化数据
checks.py                  规则核验
evidence_utils.py          原文片段与来源指纹
campaigns.py / intake.py    任务与需求补问
inspiration.py             选题搜索
covers.py / cover_titles.py 封面排版与标题检查
exporter.py                文案、原图与核验导出
assets/                    界面标识图片
prompts/                   各阶段提示词
data/campaigns.json        明确标记的本地演示任务
tests/                     离线测试
tools/                     诊断、离线演示和 Node 桥接
vendor/ima-skill/           IMA 运行入口与版本信息
.env.example               无真实凭证的配置模板
```

## IMA 第三方文件

`vendor/ima-skill/ima_api.cjs` 与 `meta.json` 提取自 IMA 官方 Skill 1.1.10，运行文件未修改。官方来源：<https://app-dl.ima.qq.com/skills/ima-skills-1.1.10.zip>。

本仓库仅保留实际依赖的运行入口与版本信息，未附带官方 Skill 文档、编辑历史和其他上传工具。Python 通过 `tools/ima_bridge.cjs` 的标准输入传入正文，通过子进程环境变量传入凭证；运行时不会将整个 Skill 作为模型提示词。

官方接口若提示版本过期，请从官方来源获取新版并更新对应运行文件及版本信息。第三方文件及远端服务的使用条件以官方规定为准。

## 当前范围

- 演示任务不是企业总部正式批准的任务。真实运营需自行维护任务、审核人和有效期。
- 手机选项与资料内容来自接入的知识库，不代表本项目已独立核实产品发布、参数或销售政策；手机成稿需使用真实图片另行验收。
- 规则与模型审核都可能漏检，不承诺零幻觉，也没有几千家门店的转化效果数据。
- 不包含自动发布、自建图片生成模型、扫描 PDF 的 OCR 或多租户账号隔离；同一服务实例使用服务端配置的凭证。
- 页面会话保存在服务内存中；事实卡、演示输出和日志可能在本地产生，均已列入 Git 忽略规则。

## 仓库与本地文件

`.gitignore` 排除真实 `.env`、密钥文件、虚拟环境、缓存、日志、运行产物、个人事实卡和压缩包，仅保留空凭证模板。仓库不包含原项目 Git 历史。

本目录可作为新仓库的根目录提交。后续配置的密钥、企业资料、门店实拍和生成内容继续保留在本地，不要手动强制加入版本管理。
