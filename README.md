# content-memory-mcp

`content-memory-mcp` 是一个面向 **长期内容资产** 的 Python MCP 服务。它把两类原本散落在 skill 里的能力收拢成一套稳定的远程接口：

- **长期笔记与知识沉淀**，对应 `notes.*`
- **公众号文章抓取、归档、检索与语料复用**，对应 `weixin.*`

这个项目不是普通 CRUD 服务，也不是只会搜文件的壳。它的定位是一个 **Qdrant 驱动的 RAG 内容中台**：

- 原始内容保存在本地主库，便于回溯、导出、重建索引
- 向量索引写入 Qdrant，负责高效召回
- Embedding 服务负责把文本转成语义向量
- MCP Tools / Resources / Prompts 负责把这些能力标准化暴露给 ChatGPT 或其他 MCP 客户端

当前版本同时支持两种传输：

- **stdio**，适合本地客户端与测试
- **Streamable HTTP**，适合通过域名反向代理后给 ChatGPT 远程接入

默认启动脚本 `start.sh` 走的是 **HTTP 模式**，绑定 `127.0.0.1:5335`。

---

## 这个项目解决什么问题

很多内容系统会掉进两个坑。

第一个坑是 **内容存了，但用不起来**。笔记散在 JSON、Markdown、网页抓取结果里，真正要找时只能全文件扫描。

第二个坑是 **检索做了，但主库丢了**。只剩向量库和摘要，原始内容不完整，一换模型或切块策略就得全量重建。

这个项目的思路就是把这两件事拆开：

- **主库负责保存原文**
- **Qdrant 负责向量索引与召回**
- **MCP 负责对外提供稳定能力**

所以你拿到的不是一个“只会搜库”的 MCP，也不是一个“只会记笔记”的工具，而是一套以内容资产为中心的统一服务。

---

## 整体架构

```text
ChatGPT / 其他 MCP 客户端
        │
        ▼
远程 MCP 协议层
(Streamable HTTP / stdio)
        │
        ▼
MCP 服务层
(Tools / Resources / Prompts)
        │
        ▼
业务服务层
(NotesService / WeixinService)
        │
        ├── 主库存储层
        │   ├── notes: JSONL + catalog
        │   └── weixin: markdown/html/json/meta/registry
        │
        └── RAG 层
            ├── 文本清洗与切块
            ├── Embedding Provider
            ├── Qdrant collection 管理
            ├── 向量写入
            ├── 向量召回
            └── 文档级聚合与轻量重排
```

这套分层里，每一层只做自己该做的事：

- **远程/本地传输层** 负责 MCP 通道，不负责业务逻辑
- **MCP 层** 负责协议和工具暴露
- **服务层** 负责业务语义与输入输出统一
- **主库层** 负责真相源
- **RAG 层** 负责检索能力

这样后面你换 embedding、重建索引、接不同 MCP 客户端，都不需要把整个系统推倒重来。

---

## 为什么是 MCP，而不是继续堆 skill prompt

Skill 更像“工作流脑子”，适合承载：

- 什么时候触发
- 先做什么后做什么
- 输出格式怎么校验
- 哪些 gotchas 需要提醒

MCP 更适合承载：

- 可以被自动调用的工具
- 可读取的结构化资源
- 可复用的标准化 prompt 入口

所以这个项目没有试图把旧 skill 一比一搬运，而是把 skill 背后的 **可执行能力** 标准化。结果就是：

- 笔记写入可以直接调 `notes.add`
- 笔记 RAG 上下文可以直接调 `notes.retrieve_context`
- 公众号文章抓取可以直接调 `weixin.fetch_article`
- 公众号语料检索可以直接调 `weixin.search_articles`

这比把一大段 workflow prompt 硬塞给模型更稳，也更容易测试。

---

## 目录结构

```text
content-memory-mcp/
├── .env.example
├── README.md
├── project.md
├── install.sh
├── start.sh
├── docker-compose.qdrant.yml
├── deploy/
│   └── nginx.content-memory-mcp.conf.example
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── scripts/
│   └── install_smoke.py
├── src/content_memory_mcp/
│   ├── __init__.py
│   ├── main.py
│   ├── server.py
│   ├── http_server.py
│   ├── tooling.py
│   ├── resources.py
│   ├── prompts.py
│   ├── paths.py
│   ├── notes_utils.py
│   ├── rag.py
│   ├── services/
│   │   ├── notes.py
│   │   └── weixin.py
│   └── vendor/
│       ├── storage_json.py
│       └── weixin_lib.py
└── tests/
    ├── conftest.py
    ├── test_mcp_stdio.py
    ├── test_mcp_http.py
    ├── test_notes_service.py
    ├── test_rag_provider.py
    └── test_weixin_service.py
```

可以粗暴地理解成：

- `server.py` 是 stdio 协议入口
- `http_server.py` 是远程 HTTP MCP 壳层
- `tooling.py` 是工具注册表
- `services/` 是业务层
- `rag.py` 是检索中枢
- `vendor/` 是对原有 skill 核心能力的保留与适配
- `tests/` 是可靠性底线

---

## 数据模型与存储策略

### Notes

Notes 侧继承的是“长期记忆”那套思路：

- 原始记录写进 `raw/*.jsonl`
- 索引和摘要落在 `index/*/catalog.json`
- 每条记录带 `id / title / text / tags / created_at / updated_at / library` 等信息

默认主库路径来自环境变量：

- `CONTENT_MEMORY_MCP_NOTES_ROOT`

默认示例路径：

- `~/.openclaw/workspace/agent-memory`

### Weixin

Weixin 侧保留了公众号抓取的原始结构：

- `markdown`
- `html`
- `json`
- `meta`
- `registry`
- account/global knowledge base 文件

默认主库路径来自环境变量：

- `CONTENT_MEMORY_MCP_WEIXIN_ROOT`

默认示例路径：

- `~/.openclaw/data/mp_weixin`

### Qdrant

Qdrant 不是主库，只是 **索引层**。

当前默认按大来源拆成两个 collection：

- `content_memory_notes_chunks`
- `content_memory_weixin_chunks`

这样做是为了把 notes 和 weixin 这两类语料隔开，因为它们的来源、payload 结构、写入链路和检索场景都不同。

但它们并没有继续按“产品 / UI / 商业 / 故事”拆成四五个 collection。对这类主题分类，更合理的做法通常是：

- 放在同一个 notes collection 里
- 用 `library / category / tags / project / stage` 这类 metadata 去过滤

这比一上来把库拆成碎玻璃更稳。

### 稳定性策略

这个项目把“抓取/保存”和“后处理”分开看待。

- **抓取成功并完成落盘** 不应该因为后续的 KB 构建或 RAG 重建失败而被整体判成失败
- 后处理异常会以下面的形式返回：`warnings`
- 只有真正的抓取失败、参数错误或主库存储失败，才会返回错误

这能避免一种很恶心的情况：文章其实已经入库了，但客户端只收到 `ok=false`，误以为整次操作失败。

---

## RAG 是怎么工作的

### 写入链路

#### Notes

1. 调用 `notes.add` 或 `notes.update`
2. 写入 JSONL 主库
3. 把可检索字段拼成文本
4. 按 `chunk_size / overlap` 切块
5. 调用 Embedding Provider 生成向量
6. 把 chunk 向量和 payload 写入 Qdrant

#### Weixin

1. 调用 `weixin.fetch_article`、`weixin.fetch_album`、`weixin.fetch_history` 或 `weixin.batch_fetch`
2. 按单篇、专辑、历史消息或 manifest 批量抓取文章并落盘到 markdown/html/json/meta
3. 读取正文并纯文本化
4. 切 chunk
5. 调用 Embedding Provider 生成向量
6. 写入 Qdrant

### 查询链路

#### 文档级检索

- `notes.search`
- `weixin.search_articles`

这两个接口会：

1. 先把 query 做 embedding
2. 去 Qdrant 召回最相关 chunk
3. 再按文档聚合
4. 返回文档级 hits，并附带命中的 top chunks

#### RAG 上下文检索

- `notes.retrieve_context`
- `weixin.retrieve_context`

这两个接口直接返回 chunk 级内容，适合再喂给模型生成答案、摘要、选题、结构草稿。

补充一点，weixin 域现在已经对齐 WeSpy 的几类核心能力：

- 单篇抓取
- 专辑列表读取，相当于 `--album-only`
- 专辑批量抓取，支持 `max_articles`
- 历史消息列表读取与批量抓取
- HTML / JSON / Markdown 输出开关

---

## MCP 能力总览

### Tools

系统工具：

- `system.health`

Notes：

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

Weixin：

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

### Resources

典型资源 URI：

- `content-memory://overview`
- `content-memory://notes/today`
- `content-memory://notes/date/{date}`
- `content-memory://notes/record/{id}`
- `content-memory://weixin/accounts`
- `content-memory://weixin/account/{account_slug}`
- `content-memory://weixin/article/{account_slug}/{uid}`

### Prompts

Prompts 提供显式工作流入口，比如围绕 notes 或 weixin 的 RAG 提问模板。

---

## 安装

### 1. 一键安装

```bash
chmod +x install.sh
./install.sh
```

安装脚本会做这些事：

- 检查 Python 版本
- 创建 `.venv`
- 安装项目依赖
- 如果 `.env` 不存在则自动生成
- 检查或启动本地 Qdrant 容器
- 跑一次离线 smoke 测试

安装脚本有“已安装跳过”逻辑：

- 如果 `.venv` 已存在
- 且依赖指纹没有变化
- 且当前安装版本匹配

就不会重复安装依赖。

### 2. 配置 `.env`

把 `.env.example` 复制成 `.env` 后，至少补齐这些：

```bash
CONTENT_MEMORY_MCP_EMBEDDING_PROVIDER=openai
CONTENT_MEMORY_MCP_EMBEDDING_BASE_URL=https://your-embedding-endpoint/v1
CONTENT_MEMORY_MCP_EMBEDDING_API_KEY=your_api_key
CONTENT_MEMORY_MCP_EMBEDDING_MODEL=text-embedding-3-small
CONTENT_MEMORY_MCP_EMBEDDING_DIMENSIONS=1536
```

如果你想让服务监听固定本地端口 5335，默认已经是：

```bash
CONTENT_MEMORY_MCP_HTTP_HOST=127.0.0.1
CONTENT_MEMORY_MCP_HTTP_PORT=5335
CONTENT_MEMORY_MCP_HTTP_MCP_PATH=/mcp
CONTENT_MEMORY_MCP_HTTP_HEALTH_PATH=/healthz
```

---

## 启动

```bash
chmod +x start.sh
./start.sh
```

`start.sh` 默认会启动 **Streamable HTTP MCP 服务**，绑定：

- `127.0.0.1:5335`

可用接口：

- MCP endpoint: `http://127.0.0.1:5335/mcp`
- Health endpoint: `http://127.0.0.1:5335/healthz`

如果你只是本地调试 stdio，也可以直接运行：

```bash
.venv/bin/content-memory-mcp --env-file .env stdio
```

---

## 域名与反向代理

ChatGPT 只能接 **remote MCP server**，不能直接连接本地 stdio 服务。所以你必须把本地的 5335 端口通过 HTTPS 域名暴露出来。

项目已经附了 Nginx 示例：

- `deploy/nginx.content-memory-mcp.conf.example`

推荐映射：

- `https://mcp.yourdomain.com/mcp` -> `http://127.0.0.1:5335/mcp`
- `https://mcp.yourdomain.com/healthz` -> `http://127.0.0.1:5335/healthz`

如果你用 Cloudflare Tunnel、ngrok 或自建反代，原则也是一样：

- 对外一定要是 HTTPS
- MCP 入口路径建议固定成 `/mcp`
- 健康检查路径建议固定成 `/healthz`

---

## 在 ChatGPT 里怎么接

1. 先把服务通过域名暴露成 HTTPS
2. 在 ChatGPT 开启 Developer mode
3. 进入 Apps / Connectors 设置页
4. 创建一个 app，填入你的远程 MCP 地址，例如：

```text
https://mcp.yourdomain.com/mcp
```

连接后，在 ChatGPT 对话里不是用 slash command，而是通过自然语言或显式点名 app/tool 来调用。

例如：

- “使用 content-memory 的 `notes.add` 工具，记录这条产品想法……”
- “使用 content-memory 搜索我最近关于 RAG 的笔记”
- “使用 content-memory 抓取这篇公众号文章并建立索引……”

---

## 测试与可靠性

当前测试覆盖：

- Notes 写入 / 检索 / RAG 上下文
- Weixin 抓取 / 检索 / RAG 上下文
- MCP stdio 初始化与工具调用
- MCP HTTP 初始化、session header、工具调用、资源读取
- OpenAI 兼容 embedding 请求格式
- collection 维度不一致时报错保护

运行测试：

```bash
.venv/bin/python -m pytest -q
```

安装脚本还会跑一次离线 smoke 测试，验证：

- HTTP 服务能起来
- initialize 能返回 `Mcp-Session-Id`
- `notes.add` 正常
- `notes.search` 正常

---

## 设计边界

这个项目有几个边界是故意保留的：

1. **Qdrant 不是主库**。原文仍然存本地。
2. **notes 和 weixin 分 collection**，但 notes 内部默认不乱拆 collection。
3. **embedding 与向量库分离**。向量由 embedding 服务生成，不让 Qdrant 承担不该承担的职责。
4. **默认保留写工具**。这意味着公网暴露时你应该自己决定是否通过反向代理、网络 ACL 或后续 OAuth 做额外保护。

---

## 后续演进方向

后面最值得做的事情，不是继续堆功能，而是把下面几项逐步补强：

- 给 notes 增加更细的 metadata 过滤维度
- 给 weixin 增加更强的账号级/主题级重排
- 增加 remote MCP 的认证层
- 增加面向 ChatGPT 的更清晰 tool 描述与只读视图
- 在保持主库不动的前提下，支持重建不同 embedding 模型的索引

---

## 相关文件

- `project.md`：偏架构与设计说明
- `README.md`：偏项目说明与使用手册
- `deploy/nginx.content-memory-mcp.conf.example`：反向代理示例
