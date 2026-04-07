# content-memory-mcp 项目设计说明

## 1. 项目定位

`content-memory-mcp` 是一个把“长期笔记能力”和“公众号语料能力”统一收敛为 **MCP Server** 的项目。

它的目标不是复刻原有 skill 的所有提示词细节，而是把原来散落在 skill 里的**可执行能力**抽成稳定的工具层，让不同客户端都能通过标准接口调用。

这个项目当前主要承接两类能力：

- **knowledge-memory-router** 对应的长期笔记与知识检索能力
- **mp-weixin-corpus-builder** 对应的公众号文章抓取、归档、索引与检索能力

从工程分层上看，这个项目属于一个 **内容中台型 MCP**：

- 上层是自然语言调用和宿主侧自动路由
- 中间层是 MCP tool / resource / prompt
- 下层是本地内容主库 + 向量索引 + RAG 检索链

---

## 2. 设计目标

### 2.1 主要目标

1. **统一能力出口**  
   不再让“笔记 skill”和“公众号 skill”各自维护一套调用逻辑，而是对外暴露统一的 MCP 接口。

2. **主库与索引分离**  
   原始内容必须可回溯、可导出、可重建索引，不能把 Qdrant 当唯一真相源。

3. **直接支持 RAG**  
   项目不是只做 CRUD，而是从第一天就按“可检索、可召回、可给模型喂上下文”的路线设计。

4. **保持对原始 skill 数据结构的兼容**  
   优先复用原有 `~/.openclaw/workspace/agent-memory` 与 `~/.openclaw/data/mp_weixin` 目录，减少迁移成本。

5. **对宿主保持中立**  
   当前实现是 Python stdio MCP server，未来可迁移到 remote MCP，不锁死在单一客户端。

### 2.2 非目标

1. 不追求把原 skill 的所有 prompt 编排逻辑完全一比一搬进 MCP。
2. 不把 Qdrant 作为内容主库。
3. 不把 notes、weixin、业务文档、任意文本全部混成一个无边界的 collection。
4. 不在 1.x 版本里引入过多复杂编排，例如多阶段 agent workflow、任务编排器、异步任务系统。

---

## 3. 核心设计思想

### 3.1 MCP 只暴露能力，不承载整套 workflow 提示词

原 skill 更像“工作流脑子”，包括：

- 什么时候触发
- 先做什么再做什么
- 遇到什么情况读哪个 reference
- 输出如何校验

MCP 更适合承载的是 **可复用、可契约化、可测试的能力原语**，例如：

- 写入一条笔记
- 搜索一批笔记
- 抓取一篇公众号文章
- 重建某个账号的向量索引
- 获取一组可直接给大模型使用的上下文 chunk

因此，本项目的定位不是“skill 文件夹的远程化”，而是“skill 背后能力的标准化接口化”。

### 3.2 主库与索引层分离

这是整个项目最重要的设计原则之一。

- **主库** 负责保存原始内容
- **向量索引层** 负责召回
- **Embedding 服务** 负责语义向量生成

具体来说：

- notes 主库仍然是 JSONL + JSON catalog
- weixin 主库仍然是 markdown/html/json/meta/registry
- Qdrant 只保存 chunk 向量和必要 payload

这样设计的好处是：

1. 以后换 embedding 模型时，只需要重建索引，不需要迁内容。
2. 调整切块策略时，只需要重跑 indexing。
3. 即使 Qdrant 故障，原始知识资产仍然存在。

### 3.3 先按“来源”分 collection，而不是按“主题”盲目分库

当前项目采用的是：

- `content_memory_notes_chunks`
- `content_memory_weixin_chunks`

这样做的原因是：

1. notes 与 weixin 的数据来源、写入链路、元数据结构不同。
2. 两类内容的检索与管理边界天然不同。
3. 分 collection 可以降低 payload schema 混乱和误检索风险。

但是，项目 **没有默认把产品思路、UI 思路、商业思路、故事等继续拆成独立 collection**。这类分类更适合先通过 metadata 实现，例如：

- `category=product`
- `category=ui`
- `category=business`
- `category=story`

原因是：

- 主题之间本来就会交叉
- 过早分 collection 会提高检索合并和维护复杂度
- 小到中等规模的数据更适合“同 collection + 元数据过滤”的模式

---

## 4. 系统分层

### 4.1 接口层

接口层由 MCP 协议承载，当前实现包括：

- **Tools**：面向自动调用和显式调用的可执行动作
- **Resources**：面向读取的结构化资源入口
- **Prompts**：用于提供标准化任务模板

核心工具分为两大命名空间：

- `notes.*`
- `weixin.*`

这样做的目的是保证：

- 能力域清晰
- 权责边界明确
- 后续扩展不互相污染

### 4.2 服务层

服务层是业务语义的核心：

- `services/notes.py`
- `services/weixin.py`

这里封装的是：

- 输入参数校验
- 主库写入/读取
- 与 RAG 层的衔接
- 返回结构统一化

服务层不直接暴露给宿主，而是通过 tooling 映射到 MCP tools。

### 4.3 RAG / 索引层

`rag.py` 是整个项目的检索中枢，职责包括：

- 文本切 chunk
- Markdown 纯文本化
- Embedding Provider 适配
- Qdrant collection 初始化与校验
- 向量写入
- 向量查询
- 结果轻量重排
- 文档级聚合

这一层是项目的“检索引擎”，不是主库。

### 4.4 主库存储层

#### Notes

notes 继承了原 skill 的设计思路：

- `raw/*.jsonl` 保存原始记录
- `index/*/catalog.json` 保存索引与摘要信息

#### Weixin

weixin 继续沿用文章落盘结构：

- markdown
- html
- json
- meta
- registry
- account/global knowledge base

这样做的核心目的是：

- 保留原始内容与上下文
- 降低迁移风险
- 便于人工审查与后续导出

---

## 5. RAG 设计

### 5.1 为什么直接上真实 RAG

项目最终放弃了“hash 向量过渡方案”作为正式默认路线，原因很直接：

1. hash 向量只适合工程联调，不适合长期语义检索。
2. 一旦后续切到真实 embedding，就可能涉及 collection 维度、索引质量、召回表现的重建。
3. 既然使用者已经接受 embedding 成本，那么从第一天就固定正式路线更省事。

因此，1.0 默认生产思路就是：

**真实 embedding + Qdrant + chunk 级向量索引 + 文档级聚合检索**

### 5.2 写入链路

#### Notes 写入链路

1. 调用 `notes.add` 或 `notes.update`
2. 写入 JSONL 主库
3. 将笔记内容转成可检索文本
4. 按固定策略切 chunk
5. 调用 embedding 服务生成向量
6. 将 chunk 向量与 payload 写入 Qdrant

#### Weixin 写入链路

1. 调用 `weixin.fetch_article` / `weixin.batch_fetch`
2. 抓取文章并落盘到主库
3. 读取 markdown 正文并转纯文本
4. 切 chunk
5. 调用 embedding 服务生成向量
6. 写入 Qdrant

### 5.3 查询链路

#### 文档级查询

- `notes.search`
- `weixin.search_articles`

流程：

1. 将 query 做 embedding
2. 到 Qdrant 召回 top-k chunks
3. 按 `document_id` 聚合
4. 根据 chunk score 和 lexical score 做轻量排序
5. 返回文档级结果与 top chunks

#### 上下文检索

- `notes.retrieve_context`
- `weixin.retrieve_context`

这类接口直接返回 chunk 级上下文，目标不是列表检索，而是给后续大模型生成答案、摘要、选题、仿写等任务使用。

### 5.4 为什么要保留 lexical score

虽然主检索是向量召回，但当前实现里仍保留了轻量 lexical score，原因有两个：

1. 对实体名、标题、特定关键词，纯向量结果不一定最稳。
2. 小成本 lexical rerank 能提升结果的可解释性与稳健性。

因此，当前策略是：

- 向量召回负责找“语义相近”
- lexical score 负责补强“字面强匹配”

这是一种折中但务实的检索方案。

---

## 6. Qdrant 设计

### 6.1 为什么使用 Qdrant

Qdrant 适合承担以下职责：

- 向量点存储
- ANN 相似度搜索
- payload 过滤
- collection 管理

本项目不要求 Qdrant 成为唯一数据源，而是将它定位为 **向量召回引擎**。

### 6.2 当前 collection 设计

默认 collection 前缀由环境变量决定，最终会生成：

- `content_memory_notes_chunks`
- `content_memory_weixin_chunks`

这种设计的核心逻辑是：

- 来源不同，collection 分开
- collection 内部再通过 payload 做更细颗粒度过滤

### 6.3 是否应该按产品 / UI / 商业 / 故事分库

默认答案是：**没必要直接分成不同 collection**。

更合理的方式是：

- 在 notes collection 内增加 `category`、`project`、`stage` 等 metadata
- 查询时通过 payload filter 实现面向视角的检索

只有在下面几种情况下，才建议进一步分 collection：

1. 语义空间差异极大
2. 权限隔离要求明显
3. 生命周期策略完全不同
4. 数据规模大到影响单 collection 的维护与性能
5. 使用不同 embedding 模型或不同维度

否则，过度分库只会增加运维和检索合并成本。

---

## 7. 工具命名与使用方式

本项目没有继续保留 slash command 作为对外主交互形式，而是转换为 MCP tool 命名空间。

例如：

- `/note` → `notes.add`
- `/notes-today` → `notes.list_today`
- `/notes-fetch` → `notes.search`
- `/notes-extract` → `notes.extract`
- `/weixin-fetch` → `weixin.fetch_article`
- `/weixin-search` → `weixin.search_articles`

这样做的原因是：

1. slash command 是特定宿主的交互习惯，不是通用标准。
2. MCP 更适合以能力命名，而不是以命令行别名命名。
3. 自然语言调用时，宿主更容易根据 tool description 自动选择工具。

因此，这个项目从一开始就是朝“自然语言 + 自动工具路由”的方向设计，而不是继续固化 slash command 心智。

---

## 8. 安装与运行设计

### 8.1 为什么提供一键安装脚本

MCP 项目真正的难点不只是代码能跑，而是：

- 环境是否稳定
- 依赖是否重复安装
- Qdrant 是否已启动
- 初始自检是否通过

因此项目提供 `install.sh`，目标是把安装流程收敛成：

1. 检测环境
2. 创建虚拟环境
3. 安装依赖
4. 生成 `.env`
5. 启动或复用本地 Qdrant
6. 跑一遍 smoke check

并通过安装指纹避免重复安装。

### 8.2 为什么测试使用 mock embedding

正式运行默认建议使用真实 embedding 服务，但测试不能依赖外部 API，否则：

- CI 不稳定
- 本地测试成本高
- 排障困难

因此测试环境采用 mock embedding，只验证：

- 索引链是否通
- Qdrant 写入与查询是否通
- MCP 协议交互是否通

这保证了测试关注的是工程正确性，而不是外部服务可用性。

---

## 9. 可靠性设计

### 9.1 已有保障

当前版本已经具备以下基础保障：

1. **主库先写入**，避免只写索引不留原文
2. **collection 维度校验**，避免 embedding 维度变化把索引弄脏
3. **Qdrant 查询 fallback**，在部分未完成索引的历史内容场景下，仍可进行有限文件扫描兜底
4. **安装 smoke check**，减少“安装完成但不能用”的假象
5. **分层测试**，覆盖 notes、weixin、rag provider、MCP stdio

### 9.2 目前仍然存在的边界

项目虽然已可用，但还不是终极形态，当前边界包括：

1. 目前主要是 stdio MCP server，尚未转成 remote MCP server。
2. 公众号检索虽然已走向量召回，但大规模场景下仍值得进一步加入更完整的 rerank。
3. notes 目前更适合单 library 或轻量 metadata 检索，若后续要做多视角知识管理，建议补全更强的 filter schema。
4. 还没有引入更系统化的权限模型与多租户隔离。

---

## 10. 推荐的后续演进方向

### 10.1 近期建议

1. 为 notes 增加统一字段：
   - `category`
   - `project`
   - `stage`
   - `audience`

2. 增加多条件检索接口，例如：
   - `notes.search_all`
   - `notes.search_by_filters`

3. 为 weixin 增加更明确的 account / topic / time-range filter。

4. 为宿主补充更强的人话化 tool description，提升自动路由正确率。

### 10.2 中期建议

1. 增加 remote MCP 部署形态，使其可直接接入 ChatGPT 自定义 MCP connector。
2. 增加更强的文档级 rerank。
3. 增加批量重建、任务队列与索引状态监控。
4. 增加 dashboard 或管理界面，便于审查主库与索引状态。

### 10.3 长期建议

1. 形成统一内容资产层，不再局限于 notes + weixin。
2. 支持更多内容来源，例如网页收藏、会议纪要、产品文档、图片 OCR 结果等。
3. 逐步把“知识记录、知识组织、知识检索、知识生成”做成同一中台体系。

---

## 11. 一句话总结

这个项目的本质不是“把两个 skill 打成一个压缩包”，而是：

**把长期笔记与公众号语料的可执行能力收束为一个可测试、可重建、可扩展的 MCP + RAG 内容系统。**

它的关键设计不是“工具多不多”，而是下面这三件事：

- **原文必须保留在主库**
- **Qdrant 只做索引与召回**
- **MCP 只暴露能力，不承担宿主特有的交互形式**

如果这三件事不变，这个项目后面无论接 ChatGPT、OpenClaw、Claude Desktop，还是继续扩展成更大的内容中台，基本都不会走偏。
