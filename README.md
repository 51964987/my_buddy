# my_buddy（个人知识沉淀与 AI 工作台）

把互联网上看到的内容（社交平台 / 论坛 / 协作在线文档 / 产品文档站）沉淀为本地知识库，AI 自动整理，工作台形态未定型——因此数据管道与工作台解耦，产出任何工具都能读取的本地文件资产。

## 核心设计

- **文件系统是唯一事实源**：`kb/` 目录（Markdown + 原始件 + meta）承载全部数据，SQLite 索引可随时重建
- **入口多样，出口唯一**：浏览器扩展剪藏、手机投递、批量爬虫、飞书 API 各自接入，落盘只有一种格式
- **两种沉淀粒度**：单条流（剪藏/帖子/单篇文档）与集合同步（文档站目录树 + 增量 diff）是一等公民
- **AI 自动整理受控回流**：总结/标签/摘要卡先进 `wiki/` 隔离区（draft），人工确认后才晋升（promoted）
- **写边界守卫**：一切落盘经区域白名单 + 字段白名单 + 原子写，AI 只能写白名单区与白名单字段

## 目录结构

```
my_buddy/
├── docs/
│   ├── 方案文档.md          # 设计唯一事实源（v0.29，含修订记录）
│   └── ui-mockups/          # UI 方案示意图（5 页 + 折叠态，评审用）
├── kbserver/                # 常驻服务
│   ├── app.py               # API 网关（Capture / Collection / Query·运维 / 参数设置 / 工作台托管）
│   ├── capture.py           # 受理 + inbox 队列
│   ├── normalize.py         # 归一化引擎（trafilatura + markdownify + Playwright 动态页兜底）
│   ├── orchestrator.py      # 管道编排器（状态机 + 重试 + error/重跑 + enrich worker 并发）
│   ├── enrich.py            # AI 整理引擎（标签/摘要卡/置信度 → wiki draft；plan_entry 零落盘试跑）
│   ├── llm.py               # OpenAI 兼容客户端（chat + embeddings，provider 表 + env-only key）
│   ├── indexer.py           # 索引引擎（FTS5 + jieba + sqlite-vec 向量增强）
│   ├── curation.py          # 人工处置通道（审核台三处置 + 流水聚合，v0.17）
│   ├── sync.py              # D 类同步引擎（站点解析器注册表，P2c）
│   ├── exporter.py          # collection 全文导出（zip 中文标题布局 / merged 单文件 / pages 载荷，v0.32，只读派生）
│   ├── guard.py             # 写边界守卫（区域白名单 + 字段白名单 + 原子写）
│   ├── idgen.py             # ID/去重引擎（URL 规范化 + SHA-1）
│   ├── config.py            # 配置中心（掩码 + 原子写）
│   ├── cli.py               # 命令行投递入口
│   ├── static/              # 工作台构建产物（webui 构建输出，FastAPI 托管 /app）
│   └── __main__.py          # python -m kbserver
├── webui/                   # 工作台前端（Vite + Vue3 + TS，六页 SPA，P5）
├── tests/                   # pytest（覆盖 P0/P1/P2c/P3/P4/P5 判据与安全校验）
├── extension/               # MV3 浏览器扩展（P1：右键/快捷键剪藏 + 离线暂存重发）
├── scripts/
│   └── backup_kb.ps1        # kb/ 文件级备份（robocopy，P1）
├── .codebuddy/rules/        # AI 协作规则（核心铁律/技术栈/工作流/设计索引/UI 约束）
├── AGENTS.md                # 跨 AI 工具入口指引
└── kb/                      # 知识库数据目录（服务启动时创建）
    ├── inbox/               # 捕获暂存
    ├── sources/             # 单条沉淀（A/B/C 类）
    ├── collections/         # 文档站集合（D 类，P2）
    ├── wiki/                # AI 整理产物（隔离区，P3：draft 卡待人工晋升）
    └── index.db             # 全文/向量索引（可重建，P4）
```

## 快速开始（P0）

```powershell
pip install -r requirements.txt       # 必须用启动服务的那个解释器装；缺依赖时服务拒绝启动并报出缺哪个包（§5.1 启动期依赖自检）
python -m playwright install chromium  # 动态页兜底浏览器内核（只装一次）
.\start_kb_service.bat                 # 启动服务（读配置端口，自动清理旧实例；等效于 python -m kbserver）
.\start_kb_service.bat buildweb        # 可选：先重建工作台前端（webui/，需 Node）再启动
# 浏览器打开 http://127.0.0.1:8765/app/  # 工作台 Web UI
python -m kbserver.cli submit <url>    # 命令行投递
python -m kbserver.cli status          # 查看管道状态
python -m pytest                       # 运行测试
```

配置文件 `kbserver.config.json`（首次运行后可手工创建/经 API 修改）：`kb_root` / `host` / `port` / `token`（设置后所有 API 需带 `X-KB-Token` 头）。

## AI 整理（P3）

默认关闭。默认 provider 为 **Ollama 本地**（v0.18：`tags`/`summary_card` 指向 `http://127.0.0.1:11434/v1` + `qwen2.5:1.5b`，原生 `/api/chat` + `think:false`，零凭据零成本；GLM 预设保留，`embedding` 默认 glm）。启用步骤（§9 决策 4：provider 表 + 任务分级 + api_key env-only）：

1. 本机 Ollama 运行中且 `ollama pull qwen2.5:1.5b`（或把 provider 默认 model 改为已装模型）；GLM/云端则设置环境变量 key（永不落盘）：`$env:ZHIPUAI_API_KEY = "..."`
2. 开启 `ai.enabled = true`（工作台参数设置页、配置文件或 `PUT /api/config`，热加载）
3. 可选调整：`ai.tasks.tags` / `ai.tasks.summary_card` 各自独立 provider+model（参数设置页可分别切换并显示实际生效后端）；vLLM 以自定义 provider 条目接入（`api_key_env` 留空即可）

**触发门控（v0.29）：开总闸 ≠ 自动全量跑。** `ai.enabled` 是"允许调 LLM"的总闸，"谁来触发"由 `ai.trigger_mode` 决定（默认 `manual`）。三段式操作面（业界金丝雀 + dry-run 模式）：

| 档位 | 怎么做 | 落盘 |
|---|---|---|
| 试跑 | 文档树页点开一页 →「试跑（不落盘）」；或 `POST /api/enrich/preview?entry_id=<id>` | **零落盘**（不写 `wiki/`、不改条目、不计重试） |
| 单条 / 一批 | 「整理这一条」`POST /api/enrich/run?entry_id=<id>`；总览页「跑一批」`POST /api/enrich/run`（同步跑 `batch_size` 条，返回逐条 outcome） | 写 `wiki/` draft 卡 + 回写 `tags`/`ai.*` |
| 全量自动 | 参数设置页 → AI 模型 → `ai.trigger_mode = auto` | 同上，按 `poll_interval` × `batch_size` 周期跑 |

熔断（仅 auto 生效）：整轮无成功（`enriched == 0` 且 `retry + error > 0`）累计 `ai.breaker_threshold`（默认 3）轮即 `state=open`，静默停止自动扫描并在设置页/总览页红色提示；修正模型配置后点「恢复自动整理」（`POST /api/enrich/breaker/reset`）——程序不自动恢复，期间手动试跑/跑一批不受门控。

行为：被加工条目生成标签（回写 frontmatter `tags`）+ 摘要卡（回写 `ai.*`、`wiki/w-*.md` draft 卡），`status: enriched`。失败自动重试 `ai.max_attempts` 次，超限转 error（`error_stage: enrich`），经 `POST /api/entries/<id>/rerun` 复活。draft 卡晋升只能人工改 frontmatter `status: promoted`，无自动晋升通道。

## 全文检索与向量增强（P4）

- `GET /api/search?q=<关键词>&limit=20`：中文全文检索（jieba 预分词 + FTS5）。语料 = `sources/`、`collections/` 下 normalized/enriched 条目 + `wiki/` 全部卡片；返回命中类型（entry/wiki）、标题、URL、tags 与命中片段
- `GET /api/search?q=...&mode=semantic`：语义检索（sqlite-vec 向量 KNN，v0.17）。默认关闭——参数设置页或 API 开 `index.vector_enabled`（重启生效），embedding 走 `ai.tasks.embedding`（OpenAI 兼容 `/embeddings`，dimensions 入防混存口径，口径漂移拒绝检索并提示人工重建）
- 查询时懒同步：检索前自动比对库文件（mtime/size）增量更新索引，无需手动维护；分词口径变化自动整库重建
- `POST /api/index/rebuild`：手动全量重建（向量开启时含全量重嵌）；`kb/index.db` 可随时删除，下次检索自动重建
- 动态页兜底：剪藏/投递纯 SPA 页面且无选中文本时，自动经无头 Chromium 渲染重试（`normalize.playwright_fallback`，渲染后 HTML 存 raw，`meta.fetch.via: "playwright"`）

## 工作台（P5）

浏览器打开 `http://127.0.0.1:8765/app/`（构建产物随仓库提交，改 `webui/` 源码后 `npm run build` 重建）：

- **六页**：总览（状态/error 巡检重跑/AI 整理批量与熔断状态/索引向量状态）、检索（全文/语义切换）、时间流（过滤项来自后端枚举）、文档树（collection 目录树 + 正文阅读 + 按页「试跑（不落盘）」与「整理这一条」+ 手动同步）、审核台、参数设置
- **审核台三处置**（走 curation 守卫通道）：晋升 draft→promoted / 打回重生成（删卡 + 源条目复位 normalized）/ 删除；卡片带「AI 生成」标识与置信度；enrich 操作日志面板（meta.json `enrich.log` 聚合）
- **参数设置页**：§11.5 全分区；敏感项掩码、危险项二次确认、热生效/重启标注

## 文档站集合同步（P2c）

D 类站点级批量沉淀（§5.2 sync 实施口径）：

- `POST /api/collections`：注册 collection（`id`/`entry_url`/`toc_parser`/`library_code`），落 `collections/<id>/collection.json` 后触发全量首抓（目录树遍历 → `toc.json` 快照含每页 content_hash → 逐页落盘 `docs/<章节路径>/note.md`）
- `GET /api/collections`：清单与同步状态；`POST /api/collections/<id>/sync`：手动增量同步（树对比 + content_hash diff，仅 added/changed 页重新落盘；下架页从 toc.json 移除并保留落盘文件）
- 首个实例：火山引擎文档站（公开 JSON API，`toc_parser: "volcengine"`，`library_code` 如 `ByteHouseEnterpriseEdition`）；新站点 = 在 `kbserver/sync.py` PARSERS 注册新解析器，落盘格式不变
- 变更页与单条流同管道：落盘后进入 enrich（D 类页面量大，注册前建议确认 `ai.enabled` 与模型成本）

### 目录树导出（v0.28/v0.30/v0.32，弥补原站"只能看不能导"）

文档树页「导出」菜单，分两组：

- **目录索引**（轻量，前端本地生成）：Markdown（树 + 原站链接；v0.31 下线 CSV）
- **全文**（"目录树=大纲、正文=内联"，error 页占位"该页抓取失败"）：
  - `GET /api/collections/<id>/export?format=zip`（v0.32 **中文标题布局**）：目录标题=文件夹、页面标题=`<标题>.md`（无 frontmatter 纯原文 + 灰色原站 URL 行，同层重名加 `-2/-3`）；图片集中至各目录 `raw/`（相对引用解压离线可读）；`toc.md` 索引；全站约百 MB 级
  - 全文打包 Word（.zip，v0.32）：前端逐页转 docx 嵌图后按同名目录结构打包（`?format=pages` 载荷，zip_path 后端统一计算）
  - `?format=merged`：单文件 Markdown，正文内联、标题按大纲深度降级、每页附 `[原文](URL)`；`images=original`（默认）按 meta.json 映射把图片改写回**原站地址**（无映射回落本机 image API）；`relative`/`api` 为兼容模式
  - Word 全文（.docx 单文件）：嵌图版（v0.31）——取 merged `images=api` 变体逐图拉取嵌入（标题/列表/表格/代码块，链接可点击）；仅支持 jpg/png/gif/bmp（webp 等降级占位），单图失败不整体失败
- 存量库图片映射回填：`python scripts/migrate_image_map.py [--kb-root kb] [--collection <id>] [--apply]`（默认 dry-run；解析 raw/page.json 原件重抓对齐 sha1，经 normalize 守卫通道回写 meta.json `raw_files`：图片条目 `{path, src}`，v0.32 起新抓取自动记录）


## 技术栈

Python 3.12 · FastAPI · uvicorn · SQLite（FTS5 + sqlite-vec）· trafilatura / Playwright / markdownify · MV3 浏览器扩展

明确不引入：消息队列（inbox 目录即队列）、独立索引/向量服务、ORM。

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | schema 定稿 + 服务骨架（capture API + 归一化落盘） | 已完成 |
| P1 | 浏览器扩展 + 手机投递 + 局域网监听 | 已完成 |
| P2a/b/c | 飞书 API / 论坛抓取 / 文档站集合同步 adapter | P2c 已完成（火山引擎文档站）；P2a/b 暂缓（待凭证/待定目标站点） |
| P3 | AI 整理流水线（分级模型，重试与断点） | 已完成（默认关闭，配置后启用） |
| P4 | 全文 + 向量索引（经 Query API） | 已完成（FTS5 全文 + sqlite-vec 向量增强，向量默认关闭；含 Playwright 动态页兜底） |
| P5 | 工作台 UI（Vue3 SPA，`webui/` 工程） | 已完成（六页：总览/检索/时间流/文档树/审核台/参数设置；构建产物托管 `/app`） |

## 查阅指引

- 设计细节（需求/架构/schema/管道/部署/风险/技术决策/角色/模块）：`docs/方案文档.md`，章节索引见 `.codebuddy/rules/03-design-reference/RULE.mdc`
- 操作与验收（启动/投递/去重/error 重跑/token）：`docs/操作手册.md`
- AI 编码助手：先读 `AGENTS.md` 与 `.codebuddy/rules/`，设计变更须回写方案文档并追加修订记录

## AI 协作规则索引

规则正文在 `.codebuddy/rules/<名>/RULE.mdc`（此处只做索引，正文以规则文件为事实源）：

| 规则 | 定位 | 加载方式 |
|---|---|---|
| 00-core-principles | 三条铁律、写边界白名单、AI 内容纪律 | 总是 |
| 01-tech-stack | 技术栈必用项与禁用项、Windows 平台约束 | 总是 |
| 02-workflow | 先读设计再动手、验收对职责卡、审查循环、安全红线 | 总是 |
| 03-design-reference | 方案文档章节索引（按任务类型直查） | 按需 |
| 04-ui-constraints | 状态枚举对齐、导航折叠、AI 标识、前端禁写库 | 按需 |
