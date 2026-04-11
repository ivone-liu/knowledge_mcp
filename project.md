# content-memory-mcp 项目设计说明

## 1. 项目目标

这个项目的目标不是做一个“工具合集”，而是做一个**内容资产中台**，通过 MCP 暴露给 ChatGPT 或其他兼容客户端使用。

项目要同时满足三类内容形态：

1. **notes**：短笔记、碎片上下文、工作记忆
2. **articles**：长文归档，尤其是 PDF / EPUB / Markdown / TXT 转成正文后的内容
3. **weixin**：公众号文章、专辑和历史消息归档

这三类内容的边界必须清晰，否则后续检索、筛选、权限和维护都会变得混乱。

---

## 2. 为什么要拆成三类内容域

### 2.1 notes

`notes` 的本质是：

- 高频写入
- 内容短
- 结构轻
- 更像个人工作记忆

它适合的问题是：

- “我昨天记了什么？”
- “最近关于定价策略都记过哪些点？”
- “帮我从笔记里提炼出结论”

### 2.2 articles

`articles` 的本质是：

- 内容更长
- 来源更像文档或资料
- 更强调“保留正文”
- 更适合被 RAG 检索和复用

它对应的是：

- PDF 手册转文字归档
- EPUB 电子书局部归档
- GPT 已整理好的报告或文章
- 长篇故事、方法论、产品文档、商业分析

这一层是本次 1.3.0 新增的核心优化。

**它不是 notes 的子集。**  
把 PDF、EPUB、课程讲义都塞进 notes，只会把笔记库搞脏。

### 2.3 weixin

`weixin` 的本质是：

- 外部抓取数据源
- 有账号、专辑、历史消息等概念
- 除了正文之外，还要保留归档结构和知识库构建能力

它对应的是：

- 单篇公众号文章
- 专辑/专栏
- 历史消息列表
- 公众号语料沉淀与风格分析

---

## 3. 总体架构

```text
Client (ChatGPT / MCP client)
        |
        v
Remote MCP HTTP Server
        |
        +-- Tool Router
        +-- Resource Router
        +-- Prompt Templates
        |
        +-- NotesService
        +-- ArticleService
        +-- WeixinService
        +-- JobStore / Worker
        |
        +-- Main Storage (local files)
        +-- QdrantRAG
        +-- Embedding Provider
```

### 3.1 核心分层

#### 主库存储
负责保存：

- 原始正文
- 结构化元数据
- registry / index

这是唯一真相源。

#### Qdrant
负责保存：

- chunk 向量
- payload 元数据
- 相似度检索

它不是主库，而是召回层。

#### Embedding Provider
负责：

- 文本向量化

当前默认走 OpenAI 兼容 `/embeddings` 接口。

---

## 4. 为什么 Qdrant 不能当主库

这个判断是故意的。

如果把 Qdrant 当主库，会有几个问题：

1. payload 不是为了长期演化设计的内容模型
2. 重建 embedding 或更换 collection 时会很痛
3. 你会把“索引结构”和“内容真相源”绑死

所以这里的原则是：

- 主库保存原文
- Qdrant 保存可重建的索引

也就是说：

- embedding 模型可以换
- collection 可以重建
- 主内容不应该丢

---

## 5. 存储设计

### 5.1 notes

沿用 JSON 主库存储，适合高频轻量写入。

### 5.2 articles

`articles` 采用“**文章目录 + Markdown 正文 + meta.json + registry**”的设计。

原因：

- 比 JSONL 更适合长文正文
- 更接近 weixin 的正文归档思路
- 单篇读取更自然
- 便于后续扩展原始附件、摘要、翻译稿、衍生版本

目录结构大致是：

```text
content_articles/
  libraries/
    articles/
      <article_id>/
        article.md
        meta.json
      article-registry.json
```

### 5.3 weixin

沿用公众号域已有的归档布局，保留：

- 文章正文
- HTML / JSON / Markdown
- registry
- account KB
- global KB

---

## 6. RAG 设计

### 6.1 为什么分三个 collection

当前默认使用三个 collection：

- `content_memory_notes_chunks`
- `content_memory_articles_chunks`
- `content_memory_weixin_chunks`

这样分的原因：

1. 三类内容语料分布不同
2. payload 字段不同
3. 查询意图不同
4. 后续扩展策略不同

这比“全扔一个大 collection”更稳定，也更好调。

### 6.2 为什么不按 product / ui / business / story 分 collection

这类维度更适合做：

- `library`
- `tags`
- `category`

而不是直接拆 collection。

原因：

- 这些分类通常是业务视角，不是技术隔离边界
- 跨分类检索很常见
- 拆太细会让索引、重建和召回变复杂

所以当前建议：

- `notes/articles/weixin` 按域分 collection
- 具体主题通过 metadata 过滤

---

## 7. 为什么 articles 需要两条导入路径

远程 ChatGPT 场景有个现实问题：

**上传给 ChatGPT 的文件，不一定会原样透传给你的远程 MCP。**

所以 1.3.0 的 `articles` 设计故意做成双通道：

### 路径 A：`articles.save_text`

适合：

- GPT 已经把 PDF / EPUB / HTML 转成文本
- 现在只想把正文归档

这是最稳的路径，因为它不依赖附件透传。

### 路径 B：`articles.ingest_file` / `articles.ingest_base64`

适合：

- 你自己的集成层
- 本地部署环境
- 外部服务已经拿到了文件字节流

这条路负责直接解析：

- PDF
- EPUB
- Markdown
- TXT
- HTML

---

## 8. 为什么 articles 的文件导入走任务队列

长文导入和公众号抓取有一个共同点：

- 处理时间可能偏长
- 可能要做文件读取、解析、索引
- 不适合把所有事情都压在一次同步 HTTP 请求里

所以：

- `articles.save_text` 保持同步
- `articles.ingest_file` 走异步队列
- `articles.ingest_base64` 走异步队列
- `weixin.fetch_*` 走异步队列

这条分界线是有意的：

- 轻操作同步
- 重操作异步

---

## 9. 任务队列设计

当前 `JobStore` 的定位是：

**高健壮单机任务队列**

主要能力：

- 本地持久化
- 原子写入 job 状态
- 单 worker 串行执行
- 高频重复提交去重
- 失败重试（针对抓取类任务）
- worker 崩溃后重启恢复

### 为什么现在只做单 worker

因为你当前最需要的是：

- 稳定
- 状态一致
- 避免竞态

不是“表面并发”。

特别是公众号抓取和 KB 重建，本身就不适合粗暴多并发。

---

## 10. 为什么抓取结果不能返回本地路径

远程 MCP 返回本地绝对路径是错误设计。

原因：

1. ChatGPT 不能直接访问你服务器本地文件系统
2. 客户端容易把路径误当成资源地址
3. 高频情况下会引发 `Resource not found` 一类误报

所以当前原则是：

- tool 返回结构化摘要
- 需要读取内容时返回 `resource_uri`
- 真正内容通过 `resources/read` 获取

这也是为什么后续所有可读内容都应资源化。

---

## 11. MCP 层设计

### 11.1 tools

负责：

- 写入动作
- 检索动作
- 队列提交
- 重建索引

### 11.2 resources

负责：

- 读取正文
- 读取列表
- 读取 job 状态
- 读取账号内容

### 11.3 prompts

负责：

- 提供给客户端更自然的任务模板

这样拆开以后：

- tool 是能力原语
- resource 是上下文入口
- prompt 是自然语言工作流入口

---

## 12. 1.3.0 的新增价值

1. 新增 `articles` 内容域
2. 支持 PDF / EPUB / Markdown / TXT / HTML 导入
3. 支持“GPT 先转文字，再保存成长文”的远程稳定链路
4. 保持与 notes、weixin 并列，而不是硬塞进笔记系统
5. 文章库也接入了独立 RAG collection 和资源 URI

从系统视角看，这一步把项目从：

- “笔记 + 微信”

推进到了：

- “笔记 + 长文资料 + 微信语料”

这才像完整的内容资产中台。

---

## 13. 当前边界

当前系统已经够稳定，但边界也要说清楚。

### 当前适合

- 单机部署
- 远程 HTTP MCP
- Qdrant 做向量索引
- 一台机器独占数据目录
- Nginx / Caddy 反向代理给 ChatGPT

### 当前不适合

- 多实例共享本地 job store
- 分布式锁协调
- 多 worker 并行写同一数据目录

所以当前准确定位是：

**高健壮单机版 remote MCP 内容中台**

---

## 14. 下一步可演进方向

1. `articles` 增加原始附件留存
2. `articles` 增加更细的 metadata，如 `category/project/stage`
3. 把更多 KB 文件暴露成正式 resources
4. queue 从全局串行升级为“账号/库级串行”
5. 如需多实例，再迁到外部 job store / Redis / DB

---

## 15. 结论

整个项目的核心设计原则可以压成一句话：

**按内容域分层，把正文留在主库，把语义留给 Qdrant，把远程调用收敛到 MCP，把重操作放进队列。**

这比把所有东西都塞进一个“万能笔记库”或“万能 collection”要稳得多。
