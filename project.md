# content-memory-mcp 项目设计说明

## 1. 项目定位

`content-memory-mcp` 是一套面向远程 MCP 场景的内容服务。

它把两个原本分散的 skill 能力收敛成一套可部署、可检索、可被 ChatGPT 调用的服务：

- 长期笔记能力
- 微信公众号抓取与知识库能力

目标不是复制 skill 的提示词，而是提炼出更稳定的基础设施层：

- 统一的 content API
- 稳定的异步抓取
- 主库与向量索引分层
- MCP tools / resources / prompts 暴露

---

## 2. 为什么不是继续堆 skill

skill 擅长做：

- 触发语义
- 默认流程
- 输出模板
- 模型的工作流引导

但 skill 不适合长期承载这些职责：

- 远程 HTTP 服务
- 高负载抓取
- 异步任务管理
- 持久化 job 状态
- 向量索引与重建
- 域名、反代、远程 ChatGPT 接入

所以项目采用的原则是：

- **skill 留在上层**，负责表达“怎么用”
- **MCP 留在下层**，负责表达“能做什么”

---

## 3. 系统分层

### 3.1 接入层

负责：
- MCP 协议
- Streamable HTTP
- stdio 兼容
- session 管理

核心文件：
- `http_server.py`
- `server.py`
- `main.py`

### 3.2 编排层

负责：
- tool 注册
- resource 暴露
- prompt 暴露
- app context 初始化
- job 队列注入

核心文件：
- `tooling.py`
- `resources.py`
- `prompts.py`

### 3.3 业务层

负责：
- notes 业务
- weixin 业务
- jobs 异步任务

核心文件：
- `services/notes.py`
- `services/weixin.py`
- `jobs.py`

### 3.4 存储层

负责：
- 本地 JSON / Markdown / HTML 持久化
- Qdrant 索引
- registry / account info / KB 文件

核心文件：
- `vendor/storage_json.py`
- `vendor/weixin_lib.py`
- `rag.py`

---

## 4. 数据边界

### 4.1 主库

主库是内容真相源。

#### notes
- JSONL / JSON catalog
- 保存原文、摘要、事实、标签

#### weixin
- Markdown 正文
- HTML 原文
- JSON 元数据
- registry / account info / reports / KB

### 4.2 Qdrant

Qdrant 只负责：

- chunk 向量存储
- 相似度检索
- payload 过滤
- 文档级聚合的召回基础

Qdrant 不负责：

- 原文真相
- 富文本展示
- 账号知识库文件管理

### 4.3 Embedding

项目默认按“真实 embedding + Qdrant”设计。

约束原则：

- 向量维度固定
- collection 按域拆开
- 不把 hash embedding 当长期方案

---

## 5. Qdrant 设计

### 5.1 为什么 notes 和 weixin 分 collection

不是为了看起来整齐，而是因为它们在三方面不同：

1. 语料结构不同
2. metadata 结构不同
3. 检索语义不同

当前默认：

- `content_memory_notes_chunks`
- `content_memory_weixin_chunks`

### 5.2 为什么不建议一开始按产品/UI/商业/故事分 collection

这些更适合做：

- `library`
- `tags`
- `category`
- `project`

而不是物理分库。

原因：

- 跨类检索更常见
- 分库会增加运维复杂度
- 小数据量时拆库反而降低检索整体性

只有在这些场景才值得单独建 collection：

- 权限隔离
- 生命周期差异大
- 不同 embedding 模型 / 维度
- 量级特别大

---

## 6. RAG 链路

### 6.1 写入

#### notes
1. 记录写入 JSON 主库
2. 组合检索文本
3. chunk
4. embedding
5. upsert 到 Qdrant

#### weixin
1. 抓取文章
2. 保存 HTML / JSON / Markdown
3. 更新 registry
4. 抽取纯文本
5. chunk
6. embedding
7. upsert 到 Qdrant

### 6.2 查询

1. query embedding
2. Qdrant top-k 检索
3. 按 doc/article 聚合
4. 返回文档级命中
5. 必要时返回 chunk 级上下文

### 6.3 重建

只要主库还在：

- 可以重切 chunk
- 可以换 embedding
- 可以重建 Qdrant

这就是主库与索引层分离的价值。

---

## 7. 为什么 1.2.0 引入任务队列

### 7.1 原同步模式的问题

原先的抓取接口把以下动作塞进一次调用：

- 网络抓取
- 文件写入
- registry 更新
- RAG 写入
- KB 构建
- 返回结果

这在远程 ChatGPT 场景里会放大四类问题：

1. 超时
2. 高频重复重建 KB
3. 返回结构混入本地路径
4. 一次失败掩盖真实入库状态

### 7.2 1.2.0 的策略

抓取类工具改成：

- 提交任务
- 返回 `job_id`
- 后台单 worker 串行执行
- 客户端通过 `jobs.get` 或 `content-memory://jobs/{job_id}` 查询

### 7.3 为什么先用单 worker

这是稳定性优先的设计。

当前公众号抓取的主要瓶颈不是 CPU，而是：

- 网络请求
- 文件 I/O
- 索引写入
- KB 构建

在这个阶段提高并发，不会线性提高吞吐，反而更容易出现账号级竞态。

所以第一阶段采用：

- 全局 FIFO
- 单 worker
- 串行执行

后续再考虑：

- account 级队列
- 不同账号并行

---

## 8. KB 的 dirty / debounce 机制

### 8.1 原问题

“每抓一篇文章就立即重建 KB” 会导致：

- 同账号重复工作
- 磁盘 I/O 激增
- 请求耗时失控
- 高频使用下状态不稳定

### 8.2 当前方案

抓取任务如果声明 `rebuild_kb=true`：

- 不同步重建 KB
- 只标记 `kb_dirty`
- 后台根据 `CONTENT_MEMORY_MCP_WEIXIN_KB_DEBOUNCE_SECONDS` 延迟创建重建任务

### 8.3 设计收益

- 抓取链路更短
- 高频写入时 KB 重建次数大幅下降
- 状态更接近“最终一致”而不是“每次都硬同步”

---

## 9. 为什么要去本地路径化

远程 ChatGPT 并不能读你服务器本地路径。

所以这些东西不应该出现在远程工具返回里：

- `local_markdown_path`
- `local_html_path`
- `local_json_path`
- `kb_dir`
- `style_profile`
- `/KB/...`

否则客户端很可能会把它误解为可读取资源，最后报：

- `Resource not found`

所以 1.2.0 的原则是：

- 对外结果只返回**资源 URI**或摘要信息
- 真正内容通过 `resources/read` 暴露

---

## 10. MCP 资源设计

### 10.1 为什么要有 jobs resource

异步抓取之后，最自然的查询方式不是继续拼工具调用，而是读取 job 资源：

- `content-memory://jobs/{job_id}`

这样客户端可以把 job 当成可读对象，而不是必须自己理解内部状态文件。

### 10.2 文章资源

文章正文资源：

- `content-memory://weixin/article/{account_slug}/{uid}`

这样抓取结果只需要返回：

- `resource_uri`

客户端按需再去读取正文。

---

## 11. 当前已知边界

### 已完成
- 远程 HTTP MCP
- notes / weixin / jobs 三域工具
- Qdrant RAG
- 微信公众号抓取队列
- WeSpy 核心能力对齐
- 返回去本地路径化
- KB dirty / debounce

### 仍然保守的地方
- 全局单 worker
- 没有完整的重试 / 死信队列
- KB 资源还没完全资源化
- 监控与指标还比较轻

---

## 12. 下一步更合理的演进方向

1. **账号级串行队列**
   - 同一账号串行
   - 不同账号并行

2. **KB 资源化补齐**
   - `style-profile`
   - `style-playbook`
   - `fulltext-analysis`

3. **失败重试与退避**
   - 抓取失败自动退避
   - 区分网络失败 / 解析失败 / 存储失败

4. **更细粒度的权限与可观测性**
   - job metrics
   - queue depth
   - per-account health

5. **更强的过滤检索**
   - category / tags / project / stage 过滤
   - notes 全局检索与多 library 检索

---

## 13. 项目判断

这个项目的核心价值，不在于“又写了一个抓公众号脚本”，而在于它把原本零散的 skill 能力，收束成了：

- 能部署
- 能反代
- 能被 ChatGPT 调用
- 能追踪状态
- 能做长期 RAG

这才是一个可持续的内容基础设施，而不是一堆脚本加几段 prompt。
