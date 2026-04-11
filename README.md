# content-memory-mcp

一个面向 **ChatGPT / 远程 MCP** 的内容中台，用来管理三类内容资产：

- **notes**：短笔记、会议记录、灵感碎片
- **articles**：长文归档，适合 PDF / EPUB / TXT / Markdown / HTML 或 GPT 整理后的正文
- **weixin**：微信公众号文章、专辑、历史消息与风格语料

这个项目不是把几个 skill 硬包成壳，而是把“可复用能力”抽成一个 **Remote MCP Server**。上层客户端用自然语言触发，底层通过 Tools / Resources / Jobs / RAG 完成写入、检索、归档和上下文召回。

---

## 1. 这个项目解决什么问题

现实里的内容资产通常不是一类东西。

一部分是碎片化信息，比如会议纪要、产品想法、临时待办。这类内容应该像工作记忆一样轻量可写。

另一部分是长文内容，比如 PDF、EPUB、报告、白皮书、手册、故事素材、GPT 已经整理好的正文。这类内容更像“文章资产”，不该和碎片笔记混在一起。

还有一部分是微信公众号内容。它不是你自己写的原始长文，而是外部语料，通常用于风格学习、案例参考、选题分析、知识沉淀。

所以这个项目把内容拆成三个域：

- `notes`
- `articles`
- `weixin`

三者共享同一套基础设施：

- 主库存储
- Qdrant 向量检索
- Embedding
- MCP 协议层
- 任务队列与健康检查

---

## 2. 总体架构

```text
ChatGPT / 其他 MCP 客户端
        |
        v
Remote MCP HTTP Server (/mcp)
        |
        +-- notes service
        +-- articles service
        +-- weixin service
        +-- jobs queue
        |
        +-- 主库存储（本地文件）
        +-- Qdrant（chunk 向量索引）
        +-- Embedding 服务（OpenAI 兼容 /embeddings）
```

分层职责是明确的：

### 主库
负责保存原始内容和结构化元数据。

### Qdrant
负责保存 chunk 向量和 payload，用于召回，不承担原文真相源角色。

### Embedding
负责把文本转成向量，不负责持久化内容。

所以这套设计里：

- Qdrant 不是主库
- 主库还在本地，可重建索引
- 向量模型只做语义表示

---

## 3. 三个内容域

### 3.1 notes

适合：

- 会议纪要
- 产品讨论点
- 临时想法
- 待办上下文
- 工作记忆

特点：

- 写入快
- 结构轻
- 以 JSON 主库为中心
- 适合短文本检索与提炼

### 3.2 articles

适合：

- PDF 提取后的长文
- EPUB 提取后的正文
- TXT / Markdown / HTML 文档
- GPT 已经整理好的长文
- 手册、报告、课程讲义、故事素材

特点：

- 独立于 notes
- 以“文章正文”而不是“碎片笔记”为中心
- 有独立存储根目录和独立 RAG collection
- 支持文本直存，也支持文件导入

这个域就是为下面这种场景准备的：

> 我把 PDF 扔给 GPT，GPT 先转成文字，再归档成长文内容。

### 3.3 weixin

适合：

- 单篇公众号文章抓取
- 专辑抓取
- 历史消息抓取
- 账号级语料沉淀
- 风格知识库构建

特点：

- 抓取类动作默认走任务队列
- 高频调用不会同步硬跑
- 不再返回本地路径
- KB 重建采用 dirty + debounce 策略

---

## 4. RAG 设计

### 4.1 写入链路

无论是 notes、articles 还是 weixin，进入索引层的逻辑都是同一套思路：

1. 保存到主库
2. 提取正文文本
3. 切 chunk
4. 调 embedding
5. 写入 Qdrant

### 4.2 查询链路

1. 用户查询做 embedding
2. 去 Qdrant 检索 top-k chunks
3. 按 document / article 聚合
4. 返回文档级结果或 chunk 级上下文

### 4.3 Collection 设计

默认分成三个域：

- `content_memory_notes_chunks`
- `content_memory_articles_chunks`
- `content_memory_weixin_chunks`

这样做是为了避免不同来源、不同元数据结构、不同使用方式的内容互相污染。

每个 collection 内仍然通过 payload 继续筛选，例如：

- `library`
- `tags`
- `source_type`
- `account_slug`

---

## 5. 为什么 articles 不等于 notes

“长文归档”不是“长一点的笔记”。

区别在于：

- notes 更像工作记忆
- articles 更像内容资产

所以 articles 有自己的：

- 存储根目录
- 资源 URI
- RAG collection
- 导入链路
- 工具接口

这样做能避免把 PDF、EPUB、产品文档、故事素材全塞进 notes，最后把笔记库变成垃圾场。

---

## 6. 队列、健壮性与高可用设计

这部分是当前版本最重要的稳定性设计。

### 6.1 哪些动作会入队

这些动作默认不会同步硬跑：

- `weixin.fetch_article`
- `weixin.fetch_album`
- `weixin.fetch_history`
- `weixin.batch_fetch`
- `articles.ingest_file`
- `articles.ingest_base64`

这些动作会先返回：

- `job_id`
- `status=accepted`
- `resource_uri=content-memory://jobs/{job_id}`

然后由后台 worker 串行执行。

### 6.2 为什么要用队列

因为公众号抓取和长文导入都可能比较重：

- 请求时间长
- 文件处理慢
- 切 chunk + embedding 较慢
- 高频请求容易互相挤压

如果全部同步执行，会很容易出现：

- HTTP 请求超时
- 同账号重复重建 KB
- 长文处理中途失败导致整体结果不稳定

所以当前策略是：

- 重活入队
- worker 串行执行
- 客户端轮询 job 状态

### 6.3 当前队列的稳态特性

当前版本已经具备这些容错能力：

#### 任务持久化
job 状态写到磁盘，不只在内存里。

#### 原子写入
job 文件使用临时文件 + `fsync` + `os.replace`，避免半截 JSON。

#### 重启恢复
进程重启后，原本处于 `running` 的任务会恢复成 `queued`，重新入队。

#### 自动重试
对临时性错误会自动退避重试：

- 抓取类任务默认重试 3 次
- 文章导入类任务默认重试 2 次
- 内部 KB 重建默认重试 2 次

#### 高频去重
相同的抓取/导入请求在 `queued` 或 `running` 状态下不会重复入队，而是直接返回已有 `job_id`。

#### 本地路径去除
远程 MCP 返回结果里不再暴露本地绝对路径，避免 `Resource not found` 一类错误。

### 6.4 当前高可用边界

实话说，这一版是 **高健壮单机版**，不是分布式高可用集群。

也就是说，它适合：

- 一台机器
- 一个服务进程
- 一个数据目录
- 前面挂 Nginx / Caddy 反代

它现在不适合：

- 多实例同时写同一份本地数据目录
- 把本地文件 job store 当分布式消息队列用

如果以后你要升级成多实例 HA，那就要换 Redis / DB 队列和外部锁，这不是当前版本的目标。

---

## 7. MCP 能力总览

### 7.1 system / jobs

- `system.health`
- `jobs.get`
- `jobs.list`
- `jobs.cancel`

### 7.2 notes

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

### 7.3 uploads

- `uploads.get`
- `uploads.list_recent`

### 7.4 articles

- `articles.save_text`
- `articles.ingest_file`
- `articles.ingest_base64`
- `articles.ingest_pdf`
- `articles.ingest_epub`
- `articles.ingest_txt`
- `articles.list_recent`
- `articles.search`
- `articles.retrieve_context`
- `articles.get`
- `articles.rebuild_index`

### 7.5 weixin

- `weixin.fetch_article`
- `weixin.list_album_articles`
- `weixin.fetch_album`
- `weixin.list_history_articles`
- `weixin.fetch_history`
- `weixin.batch_fetch`
- `weixin.list_accounts`
- `weixin.get_account_info`
- `weixin.list_arrivals`
- `weixin.search_articles`
- `weixin.retrieve_context`
- `weixin.get_article`
- `weixin.rebuild_kb`
- `weixin.rebuild_index`

---

## 8. Resources

当前暴露的核心资源包括：

- `content-memory://overview`
- `content-memory://system/health`
- `content-memory://notes/today`
- `content-memory://articles/recent`
- `content-memory://articles/library/{library}`
- `content-memory://articles/item/{library}/{article_id}`
- `content-memory://uploads/recent`
- `content-memory://uploads/item/{upload_id}`
- `content-memory://jobs/{job_id}`
- `content-memory://weixin/accounts`
- `content-memory://weixin/account/{account_slug}`
- `content-memory://weixin/article/{account_slug}/{uid}`

这些资源都是远程 MCP 可读资源，不依赖你本地文件路径暴露给客户端。

---

## 9. 配置

主要配置在 `.env` 中。

可先复制：

```bash
cp .env.example .env
```

### 9.1 存储根目录

```bash
CONTENT_MEMORY_MCP_NOTES_ROOT=~/.openclaw/workspace/agent-memory
CONTENT_MEMORY_MCP_ARTICLES_ROOT=~/.openclaw/data/content_articles
CONTENT_MEMORY_MCP_WEIXIN_ROOT=~/.openclaw/data/mp_weixin
```

### 9.2 Qdrant

```bash
CONTENT_MEMORY_MCP_QDRANT_MODE=server
CONTENT_MEMORY_MCP_QDRANT_URL=http://127.0.0.1:6333
CONTENT_MEMORY_MCP_QDRANT_API_KEY=
CONTENT_MEMORY_MCP_QDRANT_COLLECTION_PREFIX=content_memory
```

### 9.3 Embedding

```bash
CONTENT_MEMORY_MCP_EMBEDDING_PROVIDER=openai
CONTENT_MEMORY_MCP_EMBEDDING_BASE_URL=https://your-embedding-endpoint/v1
CONTENT_MEMORY_MCP_EMBEDDING_API_KEY=replace_me
CONTENT_MEMORY_MCP_EMBEDDING_MODEL=text-embedding-3-small
CONTENT_MEMORY_MCP_EMBEDDING_DIMENSIONS=1536
```

### 9.4 HTTP 服务

```bash
CONTENT_MEMORY_MCP_HTTP_HOST=127.0.0.1
CONTENT_MEMORY_MCP_HTTP_PORT=5335
CONTENT_MEMORY_MCP_HTTP_MCP_PATH=/mcp
CONTENT_MEMORY_MCP_HTTP_UPLOAD_PATH=/uploads
CONTENT_MEMORY_MCP_HTTP_UPLOAD_FORM_PATH=/upload
CONTENT_MEMORY_MCP_HTTP_HEALTH_PATH=/healthz
CONTENT_MEMORY_MCP_UPLOAD_MAX_MB=50
```

### 9.5 队列与重试

```bash
CONTENT_MEMORY_MCP_WEIXIN_KB_DEBOUNCE_SECONDS=45
CONTENT_MEMORY_MCP_JOB_FETCH_MAX_ATTEMPTS=3
CONTENT_MEMORY_MCP_JOB_ARTICLE_MAX_ATTEMPTS=2
CONTENT_MEMORY_MCP_JOB_INTERNAL_MAX_ATTEMPTS=2
CONTENT_MEMORY_MCP_JOB_RETRY_BACKOFF_SECONDS=1
CONTENT_MEMORY_MCP_JOB_RETRY_BACKOFF_MULTIPLIER=2
```

---

## 10. 安装与启动

### 10.1 一键安装

```bash
./install.sh
```

安装脚本会做这些事：

- 检查 Python 版本
- 创建 `.venv`
- 安装依赖
- 自动判断是否已安装完成，已安装则跳过
- 检查或拉起本地 Qdrant
- 运行离线 smoke 测试

### 10.2 启动服务

```bash
./start.sh
```

启动后默认监听：

- `127.0.0.1:5335`

可用健康检查：

- `http://127.0.0.1:5335/healthz`

MCP 路径默认是：

- `http://127.0.0.1:5335/mcp`

上传入口默认是：

- `http://127.0.0.1:5335/upload`
- `http://127.0.0.1:5335/uploads`

---

## 11. 域名与反向代理

如果你要给 ChatGPT 远程使用，应该把域名反代到本地 5335 端口。

推荐：

- 外部：`https://your-domain.com/mcp`
- 内部：`http://127.0.0.1:5335/mcp`

项目里已经提供示例：

- `deploy/nginx.content-memory-mcp.conf.example`

建议反代这两个路径：

- `/mcp`
- `/upload`
- `/uploads`
- `/healthz`

---

## 12. 在 ChatGPT 里怎么用

这里有个认知要纠正：

**ChatGPT 不是通过 slash command 调 MCP。**

正常方式是：

- 把这个服务配置成远程 MCP app / connector
- 在 ChatGPT 对话里通过自然语言触发

也就是说，你不是输入 `/note`，而是直接说：

- “把这段会议结论记到 notes”
- “把这份 PDF 转成正文后存到 articles”
- “抓取这个公众号链接并归档”
- “基于我最近保存的文章，帮我提炼产品思路”

### 推荐的使用策略

#### 场景 A：ChatGPT 已经拿到了 PDF / EPUB 内容
最稳的方式是：

1. 先让 GPT 读文件
2. GPT 把正文整理出来
3. 调 `articles.save_text`

这是最稳的，因为不依赖附件字节流透传。

#### 场景 B：你的服务端已经拿到文件
可以用：

- `articles.ingest_file`
- `articles.ingest_base64`

其中：

- `ingest_file` 更适合本地部署场景
- `ingest_base64` 更适合外部系统转交文件字节流

但要注意，`ingest_base64` 会让任务 payload 变大，不适合拿来当主路径滥用。能用 `save_text` 时，优先 `save_text`。

#### 场景 C：ChatGPT 拿到了文件，但拿不到服务器本地路径
可以用：

1. 先把文件上传到你的服务端 `POST /uploads`，或直接打开 `/upload` 页面选文件
2. 拿到返回的 `upload_id`
3. 再让 ChatGPT 调 `articles.ingest_pdf` / `articles.ingest_epub` / `articles.ingest_txt`，参数里只传 `upload_id`

这样 ChatGPT 不需要知道服务器本地目录，也不需要把整个文件转成超长 Base64。

---

## 13. PDF / EPUB / TXT 导入建议

### PDF
适合有真实正文、提取质量较好的文档。

### EPUB
适合电子书、合集、章节文档。系统会提取章节正文并归档为 Markdown 风格正文。

### TXT
适合纯文本长文、转写内容、日志整理结果。

### 推荐顺序

对远程 ChatGPT 场景，推荐顺序是：

1. `articles.save_text`
2. `upload_id + articles.ingest_pdf/epub/txt`
3. `articles.ingest_file`
4. `articles.ingest_base64`

不是因为后两者不能用，而是因为远程聊天场景里，**文本直存最稳定、最可控、最省事**。

---

## 14. 测试与审计范围

项目内置了多组测试，覆盖：

- notes 主流程
- weixin 单篇 / 专辑 / history / 队列 / 稳定性
- articles 文本保存、文件导入、Base64 导入、队列导入
- MCP stdio
- MCP HTTP
- job 重试与幂等

另外安装脚本还会跑离线 smoke，自检 HTTP 服务、MCP 初始化和基础写读链路。

---

## 15. 当前边界

当前版本已经适合：

- 个人部署
- 单机部署
- ChatGPT 远程 MCP 接入
- 需要 notes / articles / weixin 三域内容管理
- 需要真实 RAG 和任务队列

当前版本还不打算解决：

- 多实例共享本地目录的分布式 HA
- 外部消息队列
- 分布式锁
- 多 worker 并发写同一内容域

如果未来真要上那一层，就不是“补个小版本”了，而是要把 job store 和锁机制换成外部组件。

---

## 16. 项目文件说明

- `src/content_memory_mcp/`：核心代码
- `project.md`：项目整体设计说明
- `.env.example`：环境变量示例
- `install.sh`：一键安装脚本
- `start.sh`：启动脚本
- `deploy/nginx.content-memory-mcp.conf.example`：反代配置示例
- `scripts/install_smoke.py`：安装自检脚本
- `tests/`：测试集

如果你要看系统设计，不看版本说明，优先读：

1. `README.md`
2. `project.md`


## 显式文件导入接口

除了通用的 `articles.ingest_file` 与 `articles.ingest_base64`，项目还显式提供下面 3 个面向文档类型的工具，便于在 ChatGPT 或其他 MCP 客户端里直接触发：

- `articles.ingest_pdf`
- `articles.ingest_epub`
- `articles.ingest_txt`

这 3 个工具都支持三种输入方式：

1. `file_path`：服务器本地文件路径
2. `upload_id`：先经由 HTTP 上传入口把文件接收到服务端
3. `content_base64` + `filename`：直接上传字节内容

如果你要让人工直接上传，可打开：

- `/upload`：浏览器表单上传页
- `/uploads`：HTTP multipart 上传接口

上传成功后，响应里会返回：

- `upload_id`
- `recommended_tool`
- `recommended_arguments`

推荐用法：

- PDF 先用 `articles.ingest_pdf`
- EPUB 先用 `articles.ingest_epub`
- TXT、OCR 结果、纯文本长文先用 `articles.ingest_txt`
- 如果文件内容已经被 GPT 整理成正文，再用 `articles.save_text`
