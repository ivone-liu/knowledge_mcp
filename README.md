# content-memory-mcp

一个面向 **ChatGPT 远程 MCP** 场景的内容中台。

它把两类内容能力收进同一套 MCP 服务里：

- **notes**：长期笔记 / 灵感 / 结构化记录
- **weixin**：微信公众号文章抓取、归档、检索、风格知识库与 RAG

项目的核心目标不是“把两个 skill 原样搬过来”，而是把它们背后的**可执行能力**抽成一个稳定的 MCP 服务，让 ChatGPT 能通过自然语言触发工具，再由服务完成存储、检索和 RAG 召回。

---

## 这个项目解决什么问题

### 1. 让内容能力从 skill 提示词变成真正的服务
原始 skill 更擅长表达工作流和触发语义，但不适合承载长期运行的远程抓取、存储、队列、索引和状态管理。

这个项目把这些能力下沉为 MCP：

- 抓取文章
- 写入主库
- 写入 Qdrant 向量索引
- 查询文章 / 笔记
- 提供 RAG 上下文
- 暴露 resources / prompts / tools

### 2. 适配 ChatGPT 的 remote MCP 模式
ChatGPT 不是通过 slash command 触发 skill，而是通过 remote MCP app / connector 调用工具。

所以这个项目重点解决的是：

- HTTP 远程接入
- 域名反代
- 任务队列
- 返回结构去本地路径化
- 让工具结果对 ChatGPT 真正可读

### 3. 把“内容真相源”和“向量索引层”分开
这个项目默认采用：

- **主库**：本地文件 / JSON / Markdown
- **索引层**：Qdrant
- **语义向量**：embedding 服务生成

这样后面要重建索引时，不需要迁正文，只需要重嵌入和重写 Qdrant。

---

## 整体架构

```text
ChatGPT / 其他 MCP Client
        |
        |  HTTPS
        v
Nginx / Caddy / Cloudflare Tunnel
        |
        v
content-memory-mcp (FastAPI + MCP Streamable HTTP)
        |
        +-- notes tools/resources
        +-- weixin tools/resources
        +-- jobs tools/resources
        |
        +-- 主库存储（JSON / Markdown / HTML）
        +-- Qdrant 向量索引
```

### 分层职责

#### MCP 层
负责对外暴露：
- tools
- resources
- prompts
- HTTP / Streamable MCP 协议

#### service 层
负责业务逻辑：
- notes 服务
- weixin 服务
- 异步 job 队列

#### storage 层
负责落盘与索引：
- 本地主库
- Qdrant collection

---

## 数据设计

### Notes
笔记是长期内容主库，默认走：

- JSONL 原始记录
- JSON catalog 索引
- Qdrant chunk 向量索引

#### 为什么 notes 用一个 collection
当前不建议一开始把产品、UI、商业、故事拆成多个 Qdrant collection。

更合理的是：
- notes 一个 collection
- 用 `library` / `tags` / `category` 做过滤

因为你真正要解决的是“按视角检索”，不是“先把库拆碎”。

### Weixin
公众号内容默认分成单独 collection：

- `content_memory_weixin_chunks`

这样做是因为公众号文章和 notes 在语料分布、metadata 和使用方式上差异很大，混在一起只会让检索和过滤变脏。

---

## RAG 设计

### 写入链路

#### notes
1. 写入主库
2. 生成文档文本
3. 切 chunk
4. 调 embedding
5. 写入 Qdrant

#### weixin
1. 抓取公众号文章
2. 落盘到 markdown / html / json
3. 更新 registry / account info
4. 提取正文
5. 切 chunk
6. 调 embedding
7. 写入 Qdrant

### 查询链路
1. 查询词做 embedding
2. 去 Qdrant 检索 top-k chunks
3. 按 document/article 聚合
4. 返回：
   - 文档级结果
   - chunk 级上下文

### 为什么 Qdrant 不是主库
Qdrant 是向量检索层，不是正文真相源。

如果把 Qdrant 当主库，你后面一旦换 embedding 模型、调整 chunk 策略、重建 collection，内容资产就容易失真。

---

## 公众号抓取为什么改成任务队列

1.2.0 开始，`weixin.fetch_*` 不再同步执行完整抓取，而是：

1. 提交任务
2. 返回 `job_id`
3. 后台 worker 串行执行
4. 通过 `jobs.get` 或 `content-memory://jobs/{job_id}` 查看结果

### 为什么必须这么改
因为远程 MCP 场景下，同步抓取会带来四个问题：

- 请求太重，容易超时
- 高频抓取时反复重建同一个账号 KB
- 返回结果夹带本地路径，ChatGPT 无法消费
- 一次抓取里混合网络、I/O、索引和 KB，状态不稳定

### 当前队列策略
当前版本采用：

- **全局单 worker 串行执行**
- 抓取任务按提交顺序逐个跑
- 适合稳定优先的远程服务模式

这是刻意的，不是偷懒。

现在最重要的是稳定，而不是并发数看起来更大。

---

## KB 重建策略

公众号抓取和 KB 重建已经拆开。

### 现在的行为
- 抓取任务完成后，文章会保存并写入 RAG
- 如果调用时设置 `rebuild_kb=true`，不会立刻同步重建 KB
- 系统只会把账号标记为 `kb_dirty`
- 后台会按 `CONTENT_MEMORY_MCP_WEIXIN_KB_DEBOUNCE_SECONDS` 延迟重建

### 为什么这样做
因为“每抓一篇就立刻 rebuild KB”在高频使用时几乎必炸：

- 重复 I/O
- 重复分析
- 返回时间不稳定
- 账号级状态更容易竞态

---

## 远程返回为什么不再带本地路径

远程 ChatGPT 看不到你服务器上的：

- `/.../article.md`
- `/.../meta.json`
- `/KB/.../style-playbook.md`

这些路径只对本机开发有意义，对 remote MCP 客户端没有意义。

所以 1.2.0 开始，抓取类工具返回值里不再暴露这些本地路径，而是返回：

- `job_id`
- `status`
- `resource_uri`
- 文章摘要信息
- warnings

真正可读取的内容应该通过 MCP resource 暴露，比如：

- `content-memory://jobs/{job_id}`
- `content-memory://weixin/article/{account_slug}/{uid}`

---

## 稳定性与容错

这版额外补了 4 个关键保命件：

- **任务文件原子写入**：job 状态文件先写临时文件，再 `os.replace` 覆盖，避免异常中断时留下半截 JSON。
- **抓取类自动重试**：`weixin.fetch_*` 与内部 `rebuild_kb` 遇到超时、连接异常、429/5xx 等暂时性错误时，会按退避策略自动重试。
- **高频重复提交去重**：相同抓取请求在 `queued/running` 状态下不会重复入队，而是返回已有 `job_id`。
- **worker 自恢复保护**：调度循环有最外层保护，异常会落盘到 `state/worker-last-error.json`，线程不会因为一次异常直接死亡。

相关环境变量：

- `CONTENT_MEMORY_MCP_JOB_FETCH_MAX_ATTEMPTS`
- `CONTENT_MEMORY_MCP_JOB_INTERNAL_MAX_ATTEMPTS`
- `CONTENT_MEMORY_MCP_JOB_RETRY_BACKOFF_SECONDS`
- `CONTENT_MEMORY_MCP_JOB_RETRY_BACKOFF_MULTIPLIER`

---

## 主要工具

### jobs
- `jobs.get`
- `jobs.list`
- `jobs.cancel`

### notes
- `notes.add`
- `notes.list_today`
- `notes.list_by_date`
- `notes.search`
- `notes.retrieve_context`
- `notes.extract`
- `notes.get`
- `notes.get_raw`
- `notes.update`
- `notes.rebuild_index`

### weixin
- `weixin.fetch_article`（异步）
- `weixin.list_album_articles`
- `weixin.fetch_album`（异步）
- `weixin.list_history_articles`
- `weixin.fetch_history`（异步）
- `weixin.batch_fetch`（异步）
- `weixin.list_accounts`
- `weixin.get_account_info`
- `weixin.list_arrivals`
- `weixin.search_articles`
- `weixin.retrieve_context`
- `weixin.get_article`
- `weixin.rebuild_kb`
- `weixin.rebuild_index`

---

## 主要 resources

- `content-memory://overview`
- `content-memory://system/health`
- `content-memory://notes/today`
- `content-memory://notes/date/{date}`
- `content-memory://notes/record/{id}`
- `content-memory://jobs/{job_id}`
- `content-memory://weixin/accounts`
- `content-memory://weixin/account/{account_slug}`
- `content-memory://weixin/article/{account_slug}/{uid}`

---

## 安装

```bash
./install.sh
```

安装脚本会做这些事：

1. 检查 Python 版本
2. 创建 `.venv`
3. 安装依赖
4. 判断是否已经安装过，已安装则跳过
5. 拉起本地 Qdrant（如果你没有指定外部 Qdrant）
6. 跑一遍 smoke 测试

---

## 启动

```bash
./start.sh
```

默认监听：

- `127.0.0.1:5335`
- `/mcp`
- `/healthz`

这意味着它适合这样部署：

- 本地只监听 `127.0.0.1:5335`
- 域名层通过 Nginx/Caddy 反向代理
- ChatGPT 只访问你的 HTTPS 域名

---

## 域名反代

目标映射：

- `https://your-domain/mcp` -> `http://127.0.0.1:5335/mcp`
- `https://your-domain/healthz` -> `http://127.0.0.1:5335/healthz`

项目里带了示例配置：

- `deploy/nginx.content-memory-mcp.conf.example`

---

## ChatGPT 里怎么用

ChatGPT 不是靠 slash command 触发这个项目，而是：

1. 先把你的 remote MCP server 接进去
2. 在聊天里用自然语言调用

比如：

- `@content-memory 添加一条笔记：今天确认产品方案优先级`
- `@content-memory 抓取这篇公众号文章并入库：<url>`
- `@content-memory 查看这个抓取任务的结果：job_xxx`
- `@content-memory 搜索我关于 RAG 的笔记`

---

## 配置说明

看 `.env.example`。

重点配置有四类：

### 1. 主库目录
- `CONTENT_MEMORY_MCP_NOTES_ROOT`
- `CONTENT_MEMORY_MCP_WEIXIN_ROOT`

### 2. Qdrant
- `CONTENT_MEMORY_MCP_QDRANT_MODE`
- `CONTENT_MEMORY_MCP_QDRANT_URL`
- `CONTENT_MEMORY_MCP_QDRANT_COLLECTION_PREFIX`

### 3. Embedding
- `CONTENT_MEMORY_MCP_EMBEDDING_PROVIDER`
- `CONTENT_MEMORY_MCP_EMBEDDING_BASE_URL`
- `CONTENT_MEMORY_MCP_EMBEDDING_API_KEY`
- `CONTENT_MEMORY_MCP_EMBEDDING_MODEL`
- `CONTENT_MEMORY_MCP_EMBEDDING_DIMENSIONS`

### 4. HTTP / 队列
- `CONTENT_MEMORY_MCP_HTTP_HOST`
- `CONTENT_MEMORY_MCP_HTTP_PORT`
- `CONTENT_MEMORY_MCP_HTTP_MCP_PATH`
- `CONTENT_MEMORY_MCP_HTTP_HEALTH_PATH`
- `CONTENT_MEMORY_MCP_WEIXIN_KB_DEBOUNCE_SECONDS`

---

## 测试

```bash
pytest -q
```

当前测试覆盖：

- notes 写入 / 检索 / RAG
- weixin 单篇抓取
- WeSpy 对齐能力
- 稳定性回归
- MCP stdio
- MCP HTTP
- 公众号任务队列与无本地路径返回

---

## 当前边界

这个项目不是“万能 CMS”。

它当前更像一个：

- 内容归档服务
- 任务队列服务
- RAG 检索服务
- MCP 远程接入层

如果你后面要继续演进，最值得做的方向是：

1. 账号级队列，而不是全局单队列
2. 更细的 KB resource 暴露
3. 更细的权限隔离
4. 抓取失败重试与退避
5. 更完整的可观测性

---

## 设计文档

更完整的架构说明见：

- `project.md`
