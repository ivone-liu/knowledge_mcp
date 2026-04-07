# content-memory-mcp

`content-memory-mcp` 是一个围绕“长期内容资产”构建的 Python MCP Server。它把两类原本分散在 skill 里的能力，收敛成一套稳定、可测试、可扩展的接口层：

- **长期笔记与知识沉淀**，对应 `notes.*`
- **公众号文章抓取、归档、检索与语料复用**，对应 `weixin.*`

项目的核心目标不是做一个普通 CRUD 服务，而是做一个**可直接用于 RAG 的内容中台**：

- 原始内容保存在本地主库，便于回溯、导出、重建索引
- 向量索引写入 Qdrant，负责高效召回
- Embedding 服务负责把文本转成可检索的语义向量
- MCP Tools / Resources / Prompts 负责把这些能力标准化暴露给宿主

这意味着它既能做“保存一条笔记”这种基础动作，也能做“围绕一个主题召回上下文，再给模型回答”这种 RAG 场景。

---

## 这个项目解决什么问题

很多内容系统都会踩进两个坑。

第一个坑是**内容存了，但用不起来**。笔记散在 JSON、Markdown、网页抓取结果里，真正要找时只能全文扫文件。

第二个坑是**检索做了，但主库丢了**。只剩向量库和摘要，原始内容不完整，后续一换模型或切块策略就要重建一切。

这个项目的设计思路就是把这两件事拆开：

- **主库负责保存原文**
- **Qdrant 负责向量索引与召回**
- **MCP 负责对外提供稳定能力**

所以你拿到的不是一个“只会搜库”的 MCP，也不是一个“只会记笔记”的工具，而是一套以内容资产为中心的统一服务。

---

## 项目整体架构

```text
自然语言 / MCP 客户端
        │
        ▼
MCP 协议层
(Tools / Resources / Prompts)
        │
        ▼
服务层
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

这套分层里，每一层都只做自己该做的事：

- **MCP 层** 负责协议与接口，不负责业务细节
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

所以这个项目没有试图把旧 skill 一比一搬运，而是把 skill 背后的**可执行能力**标准化。结果就是：

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
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── scripts/
│   └── install_smoke.py
├── src/content_memory_mcp/
│   ├── __init__.py
│   ├── main.py
│   ├── server.py
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
    ├── test_notes_service.py
    ├── test_rag_provider.py
    └── test_weixin_service.py
```

可以粗暴地理解成：

- `server.py` 是协议入口
- `tooling.py` 是工具注册表
- `services/` 是业务层
- `rag.py` 是检索中枢
- `vendor/` 是对原有 skill 核心能力的保留与适配
- `tests/` 是可靠性底线

---

## 数据模型与存储策略

### 1. Notes

Notes 侧继承的是“长期记忆”那套思路：

- 原始记录写进 `raw/*.jsonl`
- 索引和摘要落在 `index/*/catalog.json`
- 每条记录带 `id / title / text / tags / created_at / updated_at / library` 等信息

默认主库路径来自环境变量：

- `CONTENT_MEMORY_MCP_NOTES_ROOT`

默认示例路径是：

- `~/.openclaw/workspace/agent-memory`

### 2. Weixin

Weixin 侧保留了公众号抓取的原始结构：

- `markdown`
- `html`
- `json`
- `meta`
- `registry`
- account/global knowledge base 文件

默认主库路径来自环境变量：

- `CONTENT_MEMORY_MCP_WEIXIN_ROOT`

默认示例路径是：

- `~/.openclaw/data/mp_weixin`

### 3. Qdrant

Qdrant 不是主库，只是**索引层**。

当前默认按大来源拆成两个 collection：

- `content_memory_notes_chunks`
- `content_memory_weixin_chunks`

这样做是为了把 notes 和 weixin 这两类语料隔开，因为它们的：

- 来源不同
- payload 结构不同
- 写入链路不同
- 检索使用场景不同

但它们并没有继续按“产品 / UI / 商业 / 故事”拆成四五个 collection。对这类主题分类，更合理的做法通常是：

- 放在同一个 notes collection 里
- 用 `library / category / tags / project / stage` 这类 metadata 去过滤

这比一上来把库拆成碎玻璃更稳。

---

## RAG 是怎么工作的

### 写入链路

#### Notes 写入

1. 调用 `notes.add` 或 `notes.update`
2. 写入 JSONL 主库
3. 把可检索字段拼成文本
4. 按 `chunk_size / overlap` 切块
5. 调用 Embedding Provider 生成向量
6. 把 chunk 向量和 payload 写入 Qdrant

#### Weixin 写入

1. 调用 `weixin.fetch_article` 或 `weixin.batch_fetch`
2. 抓取文章并落盘到 markdown/html/json/meta
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
2. 去 Qdrant 召回最相关 chunks
3. 再按文档聚合
4. 结合轻量 lexical score 做排序
5. 返回“文档级结果 + top chunks”

#### 上下文检索

- `notes.retrieve_context`
- `weixin.retrieve_context`

这两个接口直接返回 chunk 级上下文，适合给后续模型做：

- 回答问题
- 提炼摘要
- 生成选题
- 风格分析
- 二次创作

### 为什么保留 lexical score

因为纯向量召回对一些场景不够稳，尤其是：

- 实体名
- 标题词
- 短 query
- 强关键词约束

所以当前实现保留了一层轻量 lexical score，作为低成本稳态增强，而不是盲信“全向量就一定更聪明”。

---

## 当前支持的 MCP 能力

### Tools

#### system

- `system.health`

#### notes

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

#### weixin

- `weixin.fetch_article`
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

固定资源：

- `content-memory://overview`
- `content-memory://system/health`
- `content-memory://notes/today`
- `content-memory://weixin/accounts`

资源模板：

- `content-memory://notes/date/{date}`
- `content-memory://notes/record/{id}`
- `content-memory://weixin/account/{account_slug}`
- `content-memory://weixin/article/{account_slug}/{uid}`

### Prompts

- `capture_note`
- `find_notes`
- `ask_notes_rag`
- `archive_weixin_article`
- `ask_weixin_rag`

Prompts 的作用不是代替工具，而是给宿主一个更自然的任务入口。

---

## 配置方式

项目通过 `.env` 控制主库、Qdrant 和 embedding。

### 主库路径

```bash
CONTENT_MEMORY_MCP_NOTES_ROOT=~/.openclaw/workspace/agent-memory
CONTENT_MEMORY_MCP_WEIXIN_ROOT=~/.openclaw/data/mp_weixin
```

### Qdrant 配置

```bash
CONTENT_MEMORY_MCP_QDRANT_MODE=server
CONTENT_MEMORY_MCP_QDRANT_URL=http://127.0.0.1:6333
CONTENT_MEMORY_MCP_QDRANT_API_KEY=
CONTENT_MEMORY_MCP_QDRANT_COLLECTION_PREFIX=content_memory
CONTENT_MEMORY_MCP_QDRANT_TIMEOUT=10
CONTENT_MEMORY_MCP_RESET_ON_DIMENSION_MISMATCH=false
```

### RAG 切块参数

```bash
CONTENT_MEMORY_MCP_RAG_CHUNK_SIZE=500
CONTENT_MEMORY_MCP_RAG_CHUNK_OVERLAP=80
```

### Embedding 配置

```bash
CONTENT_MEMORY_MCP_EMBEDDING_PROVIDER=openai
CONTENT_MEMORY_MCP_EMBEDDING_BASE_URL=https://your-embedding-endpoint/v1
CONTENT_MEMORY_MCP_EMBEDDING_API_KEY=replace_me
CONTENT_MEMORY_MCP_EMBEDDING_MODEL=text-embedding-3-small
CONTENT_MEMORY_MCP_EMBEDDING_DIMENSIONS=1536
CONTENT_MEMORY_MCP_EMBEDDING_TIMEOUT=20
CONTENT_MEMORY_MCP_EMBEDDING_RETRIES=3
CONTENT_MEMORY_MCP_EMBEDDING_RETRY_BACKOFF_SECONDS=1.2
CONTENT_MEMORY_MCP_EMBEDDING_MAX_BATCH_TEXTS=64
```

正式环境应该直接使用真实 embedding。测试和安装自检里会使用 mock provider，但那只是为了让离线验证稳定，不应该当成正式方案。

---

## 安装与启动

### 一键安装

在项目根目录执行：

```bash
./install.sh
```

安装脚本会做这些事：

1. 检查 Python 版本是否满足 3.9+
2. 创建 `.venv`
3. 安装运行依赖和测试依赖
4. 依据依赖指纹判断是否需要重复安装
5. 自动生成 `.env`
6. 若未配置外部 Qdrant，则启动本地 Docker Qdrant
7. 运行离线 smoke 测试，验证安装结果

脚本是幂等的，已安装且依赖未变化时会跳过重复安装。

### 启动服务

```bash
./start.sh
```

也可以直接执行：

```bash
.venv/bin/content-memory-mcp --env-file /absolute/path/to/.env
```

`main.py` 当前支持 `--env-file` 参数，会在服务启动前把对应环境变量加载进进程。

---

## 如何接入 MCP 客户端

这是一个 **stdio MCP server**。适合本地 MCP 客户端、本地代理或支持 stdio 的宿主。

通用配置方式如下：

```json
{
  "mcpServers": {
    "content-memory": {
      "command": "/absolute/path/to/content-memory-mcp/.venv/bin/content-memory-mcp",
      "args": [
        "--env-file",
        "/absolute/path/to/content-memory-mcp/.env"
      ]
    }
  }
}
```

如果你的客户端不方便传参，也可以直接把命令指向：

```bash
/absolute/path/to/content-memory-mcp/start.sh
```

这套项目当前聚焦的是本地 stdio 形态。要接 ChatGPT 这类需要 remote MCP 的宿主，还需要额外做一层远程化部署，这不在当前代码内。

---

## 怎么使用这个项目

这个项目最常见的使用方式有三类。

### 1. 记录与沉淀

- 用 `notes.add` 写入会议纪要、产品灵感、故事片段、商业思考
- 用 `notes.list_today` 或 `notes.list_by_date` 查看记录
- 用 `notes.update` 修订内容

### 2. 检索与提炼

- 用 `notes.search` 找相关笔记
- 用 `notes.extract` 做简单提炼
- 用 `notes.retrieve_context` 直接拿 chunk 级上下文给模型做问答或总结

### 3. 公众号语料归档与复用

- 用 `weixin.fetch_article` 抓单篇文章
- 用 `weixin.batch_fetch` 批量抓取
- 用 `weixin.search_articles` 搜索语料
- 用 `weixin.retrieve_context` 做公众号知识 RAG
- 用 `weixin.rebuild_index` 为历史文章重建向量索引

如果你关心的是“按产品/UI/商业/故事拆视角”，建议先通过 notes 的 `library / tags / category` 来做过滤，而不是一开始把 Qdrant collection 拆得过细。

---

## 可靠性与测试

这个项目不是只写了 happy path。

当前测试覆盖包括：

- Notes 写入、检索、上下文召回
- Weixin 抓取、落盘、索引、检索
- MCP stdio initialize / tools / resources / prompts
- OpenAI 兼容 embedding provider 的请求格式、批处理和 dimensions 参数
- Qdrant collection 维度不一致时的显式报错

运行测试：

```bash
source .venv/bin/activate
pytest -q
```

### 可靠性设计要点

- **主库和索引分离**：Qdrant 出问题时，原始内容仍然在
- **安装幂等**：重复执行 `install.sh` 不会无脑重装
- **维度保护**：embedding 维度和 collection 不一致时会直接报错
- **可重建索引**：notes / weixin 都支持 `rebuild_index`

这套设计的重点不是“永不出错”，而是**出错时不要把数据和索引一起拖死**。

---

## 设计边界

这个项目当前明确不做几件事：

1. 不把 Qdrant 当主库
2. 不把所有主题都拆成独立 collection
3. 不把原 skill 的完整提示词编排原样迁入 MCP
4. 不内置远程部署层
5. 不做复杂的多 agent 编排系统

这些不是缺陷，而是刻意收住的边界。项目先把“内容保存、索引、召回、标准化调用”这条主干打稳，后面再扩。

---

## 进一步阅读

- `project.md`：更偏架构和设计原则
- `src/content_memory_mcp/rag.py`：RAG、Qdrant、embedding 的核心实现
- `src/content_memory_mcp/services/`：notes 与 weixin 的业务层
- `tests/`：当前可用能力的回归测试

如果你只看一个文件来理解整体，请先看 `project.md`；如果你要动实现，请先看 `rag.py` 和 `services/`。
