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
│   ├── 方案文档.md          # 设计唯一事实源（v0.15，含修订记录）
│   └── ui-mockups/          # UI 方案示意图（5 页 + 折叠态，评审用）
├── kbserver/                # 常驻服务（P0 已落地）
│   ├── app.py               # API 网关（Capture / Query·运维 / 参数设置）
│   ├── capture.py           # 受理 + inbox 队列
│   ├── normalize.py         # 归一化引擎（trafilatura + markdownify）
│   ├── orchestrator.py      # 管道编排器（状态机 + 重试 + error/重跑 + enrich worker）
│   ├── enrich.py            # AI 整理引擎（标签/摘要卡 → wiki draft，P3）
│   ├── llm.py               # OpenAI 兼容客户端（provider 表 + env-only key，P3）
│   ├── indexer.py           # 索引引擎（FTS5 全文检索 + jieba 预分词，P4）
│   ├── guard.py             # 写边界守卫（区域白名单 + 字段白名单 + 原子写）
│   ├── idgen.py             # ID/去重引擎（URL 规范化 + SHA-1）
│   ├── config.py            # 配置中心（掩码 + 原子写）
│   ├── cli.py               # 命令行投递入口
│   └── __main__.py          # python -m kbserver
├── tests/                   # pytest（覆盖 P0/P1/P3/P4 判据与安全校验）
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
pip install -r requirements.txt
.\start_kb_service.bat                 # 启动服务（读配置端口，自动清理旧实例；等效于 python -m kbserver）
python -m kbserver.cli submit <url>    # 命令行投递
python -m kbserver.cli status          # 查看管道状态
python -m pytest                       # 运行测试
```

配置文件 `kbserver.config.json`（首次运行后可手工创建/经 API 修改）：`kb_root` / `host` / `port` / `token`（设置后所有 API 需带 `X-KB-Token` 头）。

## AI 整理（P3）

默认关闭。启用步骤（§9 决策 4：provider 表 + 任务分级 + api_key env-only）：

1. 设置环境变量（key 永不落盘）：`$env:ZHIPUAI_API_KEY = "..."`（或换成自定义 provider 的环境变量名）
2. 配置 `ai.enabled = true`（改配置文件或 `PUT /api/config`，热加载）
3. 可选调整：`ai.tasks.tags` / `ai.tasks.summary_card` 各自指向不同 provider+model（打标签用便宜快模型、摘要卡用强模型）；Ollama/vLLM 以自定义 provider 条目接入（`api_key_env` 留空即可）

行为：服务后台周期扫描 `status: normalized` 条目 → 生成标签（回写 frontmatter `tags`）+ 摘要卡（回写 `ai.*`、`wiki/w-*.md` draft 卡）。失败自动重试 `ai.max_attempts` 次，超限转 error（`error_stage: enrich`），经 `POST /api/entries/<id>/rerun` 复活；也可 `POST /api/enrich/run?entry_id=<id>` 手动触发单条。draft 卡晋升只能人工改 frontmatter `status: promoted`，无自动晋升通道。

## 全文检索（P4）

- `GET /api/search?q=<关键词>&limit=20`：中文全文检索（jieba 预分词 + FTS5）。语料 = `sources/`、`collections/` 下 normalized/enriched 条目 + `wiki/` 全部卡片；返回命中类型（entry/wiki）、标题、URL、tags 与命中片段
- 查询时懒同步：检索前自动比对库文件（mtime/size）增量更新索引，无需手动维护；分词口径变化自动整库重建
- `POST /api/index/rebuild`：手动全量重建；`kb/index.db` 可随时删除，下次检索自动重建


## 技术栈

Python 3.12 · FastAPI · uvicorn · SQLite（FTS5 + sqlite-vec）· trafilatura / Playwright / markdownify · MV3 浏览器扩展

明确不引入：消息队列（inbox 目录即队列）、独立索引/向量服务、ORM。

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | schema 定稿 + 服务骨架（capture API + 归一化落盘） | 已完成 |
| P1 | 浏览器扩展 + 手机投递 + 局域网监听 | 已完成 |
| P2a/b/c | 飞书 API / 论坛抓取 / 文档站集合同步 adapter | 未开始 |
| P3 | AI 整理流水线（分级模型，重试与断点） | 已完成（默认关闭，配置后启用） |
| P4 | 全文 + 向量索引（经 Query API） | 全文检索已完成；向量增强未开始 |
| P5 | 工作台 UI（形态届时再定） | 未开始 |

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
