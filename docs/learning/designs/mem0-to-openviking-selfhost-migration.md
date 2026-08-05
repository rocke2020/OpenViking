# mem0 → OpenViking Self-Host Migration — Design Plan

> 一句话结论：在保持 `pkg/memory.Backend` 两方法契约和 `memory.raw_evidence` 审计链不变的前提下，以新的自托管 OpenViking backend 替换 mem0 backend，并通过后台 outbox、租户隔离、确定性 session 幂等和分阶段双跑控制迁移风险。

## TL;DR

eino 当前通过 `pkg/memory.Backend` 使用自托管 mem0：读路径调用 `Search`，写路径调用 `Add`。生产写入已由 Redis Stream durable outbox 从请求路径解耦，因此 OpenViking 的 session-based `create → batch messages → commit` 写入流程可以封装在一次 `Backend.Add` 中；读路径直接调用 `POST /api/v1/search/recall`，再将 `RecallEntry` 映射为 `MemoryCard`。接口替换不要求改动 eino 的核心请求编排，也不替换 PostgreSQL 中的 `memory.raw_evidence`。

本计划推荐先以自托管 OpenViking、单 worker、独立新向量集合上线。向量后端在 `opengauss` 与 `local` 之间保留上线前决策点；embedding 沿用当前本地 OpenViking 的 Ollama `qwen3-embedding:0.6b`（1024 维）配置，但 mem0 的 1024 维数据仍不得直接复用。迁移按「服务验证、backend 实现、配置化双跑、切流与回放验收」推进；OpenViking `CreateSession` 已验证支持客户端提供 `session_id`（见 §OpenViking Self-Host），确定性 session 策略可行，但 re-create 同 id 的碰撞语义仍需切流前 smoke 验证。

## Background

当前 eino agent 位于 `xkong-agent-center`，其长期记忆能力不直接绑定 mem0，而是依赖 `pkg/memory.Memory` 和 `pkg/memory.Backend` 抽象。

`Memory` 暴露：

| Operation | Contract |
|---|---|
| Recall | `Search(ctx, scope, query, k) ([]MemoryCard, error)` |
| Capture | `Add(ctx, scope, msgs, reason) error` |

该接口定义于 `xkong-agent-center/pkg/memory/memory.go:60-72`。真正的可插拔实现边界是 `Backend`：

| Property | Current Meaning |
|---|---|
| Read method | Search validated input and return ranked cards |
| Write method | Accept validated messages for durable-memory extraction |
| Existing implementation | `internal/memory/mem0.Client` |
| Proposed implementation | `internal/memory/openviking.Client` |
| Caller impact | Backend selection changes; interface remains stable |

`Backend` 仅包含 `Search` 和 `Add`，定义于 `xkong-agent-center/pkg/memory/memory.go:79-82`；mem0 client 已显式实现该接口，见 `xkong-agent-center/internal/memory/mem0/client.go:218`。

当前 self-hosted mem0 的读写形态是 flat HTTP API：

| mem0 Operation | HTTP Shape |
|---|---|
| Search | `POST /search` with `query`, `filters`, `top_k` |
| Add | `POST /memories` with scoped messages and `infer: true` |
| Scope fields | `user_id`, `agent_id`, `run_id` |
| Authentication | `X-API-Key` |
| Client timeout | 120 seconds |
| Temporary errors | Timeout or HTTP status `>= 500` |
| Permanent throttling behavior | HTTP 429 classified as permanent |

Search 行为见 `xkong-agent-center/internal/memory/mem0/client.go:115-133`，Add 行为见 `:156-168`，认证头见 `:36`，错误分类见 `:48-49`、`:60-62`。

当前 mem0 部署由 `agent-memories/scripts/mem0-up.sh` 驱动，覆盖 vendored `vendor/mem0/server/docker-compose.yaml`。

| Component | Current Deployment |
|---|---|
| mem0 server | Container port 8000; dev host port 8888 |
| Relational/vector store | PostgreSQL + pgvector |
| PostgreSQL mapping | Container 8432 → 5432 |
| Collection | `memories` |
| Embedding dimension | 1024 via `MEM0_EMBEDDING_DIMS` |
| Extraction LLM | OpenAI-compatible endpoint at `LLM_BASE_URL` |
| Extraction model behavior | DeepSeek with `reasoning_effort: low` |
| Embedder | OpenAI-compatible endpoint at `OLLAMA_BASE_URL` |
| Embedding model | `qwen3-embedding:0.6b` |
| Extraction policy | `custom_instructions` limits extraction to durable user facts |

服务端还暴露 `/configure`、`/reset`、`/memories/{id}`，但 eino 的 backend 契约只使用 `/search` 和 `/memories`。运行期构造发生在 `eino_wiring.go`：

| Wiring Input | Current Source |
|---|---|
| Base URL | `MemoryConfig.Mem0URL` |
| API key | `MemoryConfig.Mem0APIKey` |
| Timeout | `MemoryConfig.TimeoutMs` |
| Environment | `MEM0_URL`, `MEM0_API_KEY`, `POSTGRES_DSN` |

mem0 client 构造于 `xkong-agent-center/internal/server/eino_wiring.go:96-100`；配置字段定义于 `xkong-agent-center/internal/conf/conf.go:217-225`，env 见 `:209`。已提交的生产 K8s 覆盖仅一条 network policy：

| Policy | Current Rule |
|---|---|
| Default posture | Default deny |
| Explicit memory access | `agent-service` may connect to `mem0-server:8000` |
| Full mem0 workload manifests | Not committed in the stated source set |

见 `xkong-agent-center/deploy/k8s/networkpolicy.yaml`。

eino 请求路径执行跨 session 召回：

```text
incoming request
      |
      v
Scope{ UserID: req.UserID, AgentID: "general", RunID: req.SessionID }
      |
      | clear RunID for cross-session recall
      v
memory.Search(ctx, recallScope, req.Message, historyLimit)
      |
      +-- success --> inject cards into request context
      +-- failure --> cards=nil; request continues
```

backend 持有 `memory pkgmemory.Memory`（`backend.go:46`）。recall scope 构造于 `:302-306`，`RunID` 在 `:319-320` 清空，Search 在 `:321` 调用。recall 失败被有意设计为非致命（cards=nil 继续）——迁移期间不得改变。

Capture 两条路径：

| Capture Mode | Behavior |
|---|---|
| Durable production path | Archive the turn and enqueue an evidence outbox event |
| Legacy inline path | Call `memory.Add` directly |
| Event reason | `"eino-turn"` |
| Event backend field | Identifies the selected memory backend |

durable 与 inline 路径实现于 `backend.go:508-531`；durable 事件构造于 `:516-518`，legacy inline Add 于 `:523-527`。

durable outbox 是 OpenViking 多调用写协议的关键兼容层：

```text
eino turn -> ArchiveTurn -> Redis Stream: memory:eino:evidence
   -> evidenceoutbox consumer
        +--> raw_evidence.BatchInsert (PostgreSQL audit, 10s timeout)
        +--> memory.Add            (memory backend target, 2m timeout)
   per-target markers; 3 retries; DLQ on exhaustion; at-least-once
```

Stream 与 DLQ 名定义于 `outbox.go:21-22`。consumer 同时持有 `EvidenceStore` 和 `Memory`，按目标独立标记成功/重试/死信，见 `consumer.go:25-35`；wiring 于 `evidence_outbox_wiring.go:77`。

raw_evidence store 后端无关：

| Property | Value |
|---|---|
| Table | `memory.raw_evidence` |
| Dedup key | `(user_id, capture_ts, turn_seq)` |
| Duplicate behavior | `ON CONFLICT DO NOTHING` |
| Backend-specific data | Event backend label only |
| Migration disposition | Preserve unchanged |

见 `pgevidence/store.go:31-60`。

**OpenViking 曾被拒**。`agent-memories/docs/investigations/agent-memories-build-vs-adopt.md §4` 此前以「太宽、非 memory-focused」拒绝 OpenViking；`memory-v1.md:148` 指出 OV 的图遍历形状与 memory-v1 不同。这一拒绝不应隐瞒，但决策语境已变：

| Previous Concern | Current Reassessment |
|---|---|
| OV broader than a memory API | 稳定 eino 契约仍只要求 Search/Add |
| Graph-shaped retrieval differs | `/api/v1/search/recall` 提供 recall 专用入口 |
| Operational scope larger | 团队拥有 `ov1` 源码并已运营 OV 基础设施 |
| mem0 is memory-focused | OV 附带 typed memories / filesystem / skills / 未来 graph 能力 |
| Adoption cost | 已承认：session 范式 + 异步抽取 + 更大服务面 |
| Service count | 可把 mem0 + pgvector + 独立 embedding 合并进 OV self-host 边界 |

替换仅在以下条件成立时正当：OV 停在 `pkg/memory.Backend` 之后、通过 memory-v1 现有 gate、不把 OV 语义泄漏进 eino 核心请求流。

## The Contract

设计严格保留公开 Go memory 契约。

| Type | Required Semantics |
|---|---|
| `Memory.Search` | Validate policy, then delegate recall |
| `Memory.Add` | Validate policy, then delegate capture |
| `Backend.Search` | Receive already validated and clamped input |
| `Backend.Add` | Receive already validated messages |
| `Scope` | Carry user, agent, run identity |
| `Message` | Carry role and content |
| `MemoryCard` | Carry human-usable memory text and relevance score |

`Memory` 方法于 `memory.go:60-72`，backend seam 于 `:79-82`。`Scope` 形状：

| Field | Meaning |
|---|---|
| `UserID` | Required, non-empty user identity |
| `AgentID` | Agent identity, currently `"general"` in eino |
| `RunID` | Session/run identity |

见 `memory.go:28-32`。Message / MemoryCard 后端无关：

| Go Type | Fields |
|---|---|
| `Message` | `Role string`, `Content string` |
| `MemoryCard` | `Memory string`, `Score float64` |
| Score interpretation | Higher = more relevant; exact scale is informational |

见 `memory.go:38-41` 与 `:49-52`。Service wrapper 在 backend 之前做校验：

| Policy | Value |
|---|---|
| Minimum K | 1 |
| Maximum K | 20 |
| Default K | 5 |
| Maximum query length | 1024 |
| Maximum messages | 10 |
| Maximum content size | 8192 |

定义于 `pkg/memory/policy.go:6-15`。wrapper 的 sentinel errors 保持权威：

| Validation Failure | Sentinel |
|---|---|
| Missing user identity | `ErrMissingUserID` |
| Empty query | `ErrEmptyQuery` |
| Oversized query | `ErrOversizeQuery` |
| Empty message list | `ErrEmptyMessages` |
| Too many messages | `ErrTooManyMessages` |
| Invalid role | `ErrInvalidRole` |
| Oversized content | `ErrOversizeContent` |

OV backend 不得重复或弱化这些检查——它接收的是已校验/已钳制的输入。`NewService(backend Backend)` 构造 service（`service.go:33`）；`Scope.UserID == ""` 时 Search/Add 在接触 backend 前返回，该不变量是 backend-switch 负路径 gate 的一部分。

目标抽象：

```text
pkg/memory.Service --validated Search/Add--> pkg/memory.Backend
        |
        +-- current  --> internal/memory/mem0.Client
        +-- target   --> internal/memory/openviking.Client
                            +-- POST /api/v1/search/recall
                            +-- CreateSession -> BatchAddMessages -> CommitSession
```

不可协商的不变量：

| Invariant | Design Requirement |
|---|---|
| Interface stability | No method added to `pkg/memory.Backend` |
| Validation ownership | `pkg/memory.Service` remains the policy boundary |
| Request resilience | Recall failure stays non-fatal in eino |
| Production capture | Durable outbox remains the primary Add caller |
| Audit history | `memory.raw_evidence` retained |
| Tenant isolation | User identity maps to tenant-scoped OV auth |
| Backend neutrality | eino code must not parse OV `RecallEntry` |
| Score handling | Backend returns higher-is-better informational scores |
| Write acceptance | Add success = OV accepted the commit, not extraction complete |
| No invented API | Only verified OV endpoints and Go SDK methods |

## OpenViking Self-Host

OpenViking 经 `openviking-server` 入口运行。CLI bootstrap 起于 `openviking_cli/server_bootstrap.py:59`，进入 server bootstrap 于 `openviking/server/bootstrap.py:126`。配置为 JSON `ov.conf`（`openviking/server/config.py`）。

| Server Setting | Verified Default |
|---|---|
| Host | `127.0.0.1` |
| Port | `1933` |
| Workers | `1` |
| Auth configuration | `auth_mode` + `root_api_key` |
| CORS | `["*"]` |
| Health endpoint | `GET /health` |

Defaults 于 `config.py:268-272`；health 于 `openviking/server/routers/system.py:70`。配置解析序：`--config` → `OPENVIKING_CONFIG_FILE` → `~/.openviking/ov.conf`（`config.py:356-378`）。

首版部署用单 worker。

| Reason | Consequence |
|---|---|
| Server default is one worker | Matches source-defined safe baseline |
| Multi-worker requires factory import string | Must not assume ordinary direct app startup |
| Local storage uses a workspace PID lock | Multi-worker + local storage risks contention |
| Lock bypass `skip_process_lock` exists | Must not be enabled merely to force multi-worker |

local storage PID lock 于 `openviking_cli/utils/config/storage_config.py:26`。任何 multi-worker 设计在针对所选存储后端实测前标 `(unverified)`。

API routers 注册于 `openviking/server/app.py:526-565`。相关 verified endpoint：

| Capability | Endpoint |
|---|---|
| Health | `GET /health` |
| Create session | `POST /api/v1/sessions` |
| Add one message | `POST /api/v1/sessions/{id}/messages` |
| Batch add messages | `POST /api/v1/sessions/{id}/messages/batch` |
| Commit session | `POST /api/v1/sessions/{id}/commit` |
| Explicit extract | `POST /api/v1/sessions/{id}/extract` |
| Find | `POST /api/v1/search/find` |
| Search | `POST /api/v1/search/search` |
| Recall | `POST /api/v1/search/recall` |
| Task polling | `GET /tasks/{id}` |

create_session 于 `sessions.py:188`，commit 于 `:426`，extract 于 `:465`，add message 于 `:476`，batch 于 `:532`。find/search/recall 分别于 `search.py:176` / `:218` / `:269`。commit Phase 1 同步归档、Phase 2 异步抽取，返回 task_id 经 `GET /tasks/{id}` 轮询（`sessions.py:426`）。

**CreateSession 支持客户端提供 session_id（已验证）**：`CreateSessionRequest.session_id: Optional[str]`（`sessions.py:129`），服务端 docstring 明示「If session_id is provided, creates a session with the given ID」（`sessions.py:188-198`）；Go SDK `CreateSession(opts *CreateSessionOptions)` 透传 `opts.SessionID`（`sdk/go/sessions.go:12-22`）。session 服务另有 exists-check（`openviking/session/session.py:682` "Check whether this session already exists in storage"）。因此确定性 session_id 策略可行；剩余未验证项是 re-create 同 id 的碰撞语义（no-op / 409 / duplicate），需切流前 smoke（见 §Open Questions）。

OV 不在专用 memory 表存记忆。session commit/extract 派生 Markdown 记忆至 `viking://user/<user_id>/memories/`。Verified 内置类目：

| Category | Location |
|---|---|
| Profile | `user/memories/profile.md` |
| Preferences | `preferences/` |
| Entities | `entities/` |
| Events | `events/` |
| Identity | `identity.md` |
| Soul | `soul.md` |
| Cases | `cases/` |
| Trajectories | `trajectories/` |
| Experiences | `experiences/` |
| Tools | `tools/` |
| Skills | `skills/` |

见 `docs/en/api/16-memory.md:6-21`。实验性 Rust CLI `ov add-memory` 是 create-session → batch-add-messages → commit 的糖，不构成独立 AddMemory 服务端 API（`crates/ov_cli/src/commands/session.rs:358-398`）。

Go SDK 支持写序列：

| Go SDK Method | Server Capability |
|---|---|
| `NewClient` | Construct client |
| `Find` / `Search` | `/search/find` / `/search/search` |
| `CreateSession` | Create session (accepts `SessionID`) |
| `AddMessage` / `BatchAddMessages` | Add message(s) |
| `CommitSession` | Archive and extract memories |
| `GetTask` | Poll task |

Client 于 `sdk/go/client.go:14`、`NewClient` 于 `:27`；retrieval 于 `retrieval.go:9`、`:47`；sessions 于 `sessions.go:12`、`:80`、`:98`、`:109`、`:123`。**Go SDK 无 Recall、无 AddMemory（已确认）**：

| Need | Design Choice |
|---|---|
| Recall | Direct HTTP `POST /api/v1/search/recall` |
| Add memory | SDK session 方法（create/batch/commit） |
| Dedicated AddMemory endpoint | Must not be assumed |
| Recall SDK extension | Optional future upstream, outside this migration |

OV 支持 verified 向量后端：

| Backend | Character |
|---|---|
| `local` | Embedded default, no external vector DB |
| `cuvs` | GPU-oriented |
| `http` | HTTP adapter |
| `opengauss` | openGauss/Postgres-family, pgvector-style |
| `qdrant` | Qdrant |
| `volcengine` | VikingDB cloud |
| `vikingdb` | Private deployment |

factory 列表于 `openviking/storage/vectordb_adapters/factory.py:17-25`；default backend `local`（`openviking_cli/utils/config/vectordb_config.py`）。**无 Milvus adapter。**

初始决策在 `opengauss` 与 `local` 之间：

| Option | Benefits | Costs |
|---|---|---|
| `opengauss` | Closest to current PostgreSQL/pgvector ops; may reuse PG-family tooling | Compatibility with the exact existing PG instance + extension set `(unverified)` |
| `local` | Simplest self-host; no external vector DB; documented standalone | Embedded state changes scaling/backup/multi-worker |
| `qdrant` | Dedicated vector service with verified adapter | Adds a service, weakens consolidation |
| Milvus | None | Unsupported (no adapter) |

推荐：若 staging probe 确认 `opengauss` 与可用 PG-family 部署兼容则优先；否则首切用 `local`；不因未来 Milvus 支持而阻塞 backend 实现；OV 向量数据放新 collection/namespace。Standalone self-host 见 `docs/en/guides/03-deployment.md:72`；quickstart 见 `docs/en/getting-started/03-quickstart-server.md`。

Embedder providers 含 `openai/azure/volcengine(default)/vikingdb/jina/ollama/gemini/voyage/dashscope/minimax/cohere/litellm/local`（`openviking_cli/utils/config/embedding_config.py`）。local embedder 经 llama-cpp-python 跑 GGUF，default `bge-small-zh-v1.5-f16`，dim 512（`openviking/models/embedder/local_embedders.py`）。

| Choice | Continuity | Operations |
|---|---|---|
| Ollama + `qwen3-embedding:0.6b` | Matches current mem0 and local OpenViking configuration; 1024 dimensions | Keeps Ollama as external dependency |
| OV local embedder | Fully standalone, simpler topology | Changes model/dim to 512 |
| Future provider switch | Possible | Full re-index (dims/models cannot mix) |

推荐首迁沿用当前本地 OpenViking 的 Ollama + `qwen3-embedding:0.6b`（1024 维）配置，并在新 collection 创建前做一次端到端 embedding probe。即使两端的模型和维度相同，mem0 的 collection 也不复用：OpenViking 需要自己的 schema、metadata、索引和租户所有权边界，fresh OV collection 强制。

OV auth 两级 account+user：

| Concept | Mechanism |
|---|---|
| Account | `account_id` |
| User | `user_id` |
| Agent/peer | `actor_peer_id` |
| Root management key | `root_key` |
| Tenant-scoped key | `user_key` |
| API key header | `X-API-Key` |
| Bearer alternative | `Authorization: Bearer` |
| Account header | `X-OpenViking-Account` |
| User header | `X-OpenViking-User` |
| Actor header | `X-OpenViking-Actor-Peer` (legacy `X-OpenViking-Agent`) |

Identity/auth 于 `openviking/server/identity.py` 与 `openviking/server/auth/__init__.py`。Admin API：`POST /admin/accounts`、`POST /admin/accounts/{id}/users`（返回 tenant-bound `user_key`）。生产运行期用 tenant-scoped user key，不用 root key。拟用稳定 account `"xkong"`（设计选择，非源码要求）。部署产物：

| Artifact | Verified Behavior |
|---|---|
| `Dockerfile` | 3-stage, EXPOSE 1933, HEALTHCHECK /health |
| Container state | `/app/.openviking` |
| Config seeding | `OPENVIKING_CONF_CONTENT` seeds `ov.conf` |
| `docker-compose.yml` | OpenViking 1933 + Caddy 1934 |
| Helm chart | `deploy/helm/openviking/` |
| systemd example | `docs/en/guides/03-deployment.md:90+` |
| Makefile | Build-only; no docker/deploy target |

见 `Dockerfile`、`docker-compose.yml`、`deploy/helm/openviking/`、`Makefile`、`docs/en/guides/03-deployment.md`。

## Backend Mapping

backend adapter 吸收 API-shape 不匹配：

```text
Backend.Search --> POST /api/v1/search/recall --> RecallEntry[]
                                              | content-or-summary + score
                                              v
                                         []MemoryCard

Backend.Add --> CreateSession --> BatchAddMessages --> CommitSession
                                                       +-- Phase 1 archived synchronously
                                                       +-- Phase 2 extraction asynchronous
```

Scope 映射：

| xkong Scope | OpenViking Mapping |
|---|---|
| `Scope.UserID` | `X-OpenViking-User` |
| Stable service tenant | `X-OpenViking-Account: xkong` |
| `Scope.AgentID` | `X-OpenViking-Actor-Peer` |
| `Scope.RunID` on Add | Input to deterministic session identity |
| Empty `RunID` on Search | Cross-session recall |
| API credential | Tenant-bound `user_key` |
| Root key | Provisioning only |

OV 默认 account/user header 为 `"default"`，生产必须显式设两者以防意外跨租户收敛。Message 映射：

| `pkg/memory.Message` | OpenViking Session Message |
|---|---|
| `Role` | Session message role |
| `Content` | Session message content |
| Message order | Preserve input order |
| `reason` | 仅在有 verified session 字段时存储；否则不发明字段 `(unverified)` |
| Maximum batch | Already limited to 10 by memory policy |

结果映射：

| OpenViking RecallEntry | `MemoryCard` |
|---|---|
| `content` | Primary `Memory` candidate |
| `summary` | Fallback when content is empty |
| `score` | `Score` |
| `uri` / `type` / `mode` | Not represented in interface |
| `origin` | Used for validation/observability, not returned |
| `rank` | Preserve server ordering; not returned |
| `abstract` | Not returned |

`content` vs `summary` 的精确规则仍是 open decision。初始推荐：`content` 非空则用之，否则 `summary`；两者皆空则跳过而非产出空 `MemoryCard`。`RecallEntry` 于 `openviking/retrieve/type_quota_recall.py:42-70`，origin ∈ {`actor_peer`, `self`, `other_peer`}。

Recall 入参：

| Field | Planned Value |
|---|---|
| `query` | Backend Search query |
| `quotas` | Derived from K without exceeding K overall `(unverified mapping)` |
| `max_chars` | Explicit bounded value, initially OV default |
| `min_score` | Configurable, initially 0.1 |
| `peer_scope` | `"actor"` for AgentID-scoped recall; `"all"` only by explicit policy |
| `other_peer_penalty` | OV default unless calibration requires change |
| `render` | Selected based on response-shape smoke `(unverified)` |

RecallRequest 于 `openviking/server/routers/search.py:142-154`（`extra="forbid"`，未知字段会被拒）。OV verified recall defaults：TYPE_ORDER=`("events","entities","preferences","experiences")`，DEFAULT_QUOTAS=`{events:10, entities:10, preferences:3, experiences:0}`，DEFAULT_MAX_CHARS=6500，DEFAULT_MIN_SCORE=0.1（`type_quota_recall.py`）。

初始 Search 设计：构造 tenant+actor headers → `POST /api/v1/search/recall` → `AgentID` 非空时 `peer_scope:"actor"` → 应用配置 `min_score` → 保 server rank 序 → 丢弃语义空 entry → 至多 K 条映射 `MemoryCard` → response 校验失败即报错（不把 HTTP 200 当有效召回）→ 保留 eino 现有 Search 错误非致命处理。

K→per-type quota 映射 `(unverified)`——OV quota 按类型，`Backend.Search` 只有一个总 K。候选分配：

| K Range | Proposed Quota Strategy |
|---|---|
| 1–2 | Prefer events + entities |
| 3–5 | events + entities + preferences |
| 6–20 | Scale typed quotas, cap final at K |
| experiences | Disabled initially unless parity shows value |

此分配是设计候选 `(unverified)`，非 OV 默认。Add 设计：从 `Scope` 派生 tenant+actor → CreateSession（带确定性 `session_id`，见 §Self-Host）→ BatchAddMessages（保序）→ CommitSession → 验证 commit 接受并返回 task_id → 不等 Phase 2 抽取即返回成功 → task_id 记结构化日志/指标 → outbox 仅在 commit 接受后标记 memory 目标成功。Add 成功 = 「归档已接受、抽取已调度」，非「新记忆已可召回」。

这与生产流兼容：durable outbox 已在请求路径外以 2m 目标超时调用 Add（`consumer.go:25-35`）。时序：

```text
Turn N completes -> outbox event -> OV commit accepted -> Add returns success
   -> async extraction -> memory recallable on a LATER request
```

当前轮事实若抽取未完成则可能对紧随其后的 recall 不可见——这是已承认的行为变化，dual-run 期需度量。API-shape tensions 汇总：

| Tension | Resolution |
|---|---|
| Flat mem0 Add vs 三调用 OV write | 封装 create/batch/commit 进一次 Backend.Add |
| 同步 infer 期望 | Add commit 接受即返回；抽取异步 |
| mem0 Search 结果 vs typed RecallEntry | content/summary + score 映射 MemoryCard |
| OV 富元数据在 seam 丢失 | 仅留日志/指标；不在迁移期扩接口 |
| Cross-session recall | eino 清 RunID；backend 在 user+actor scope 内召回 |
| Agent scoping | AgentID → actor peer，`peer_scope:"actor"` |
| 无 Go Recall SDK | 直用 verified REST endpoint |
| 无 Go AddMemory SDK | 用 verified session 方法 |
| 无 Milvus adapter | `opengauss` 或 `local` |
| Embedding dim 不匹配 | fresh OV collection |
| Async extraction task | 不阻塞 Add；单独监控 `(unverified mechanism)` |
| At-least-once retry | 确定性 session_id（已验证支持）+ smoke 碰撞语义 |
| Audit 保留 | raw_evidence 不变 |
| 此前架构拒绝 | 仅在窄 Backend seam 之后接受更宽平台 |

OV 更宽能力（typed memories / filesystem / skills / graph 演进）是战略收益，不是扩张本迁移实现 scope 的许可。

## Idempotency And Recovery

durable outbox 提供 at-least-once 投递，不是 exactly-once 抽取。

| Layer | Existing Guarantee |
|---|---|
| Redis Stream | Durable event delivery |
| Consumer | Per-target success markers |
| Evidence target | Independent retry state |
| Memory target | Independent retry state |
| Retry budget | Three attempts by default |
| Exhaustion | Dead-letter queue |
| raw_evidence insert | Unique-key dedup `ON CONFLICT DO NOTHING` |
| OpenViking Add | Multi-call op; client-supplied session_id 已验证支持，re-create 碰撞语义待 smoke |

outbox 于 `outbox.go:21-22`、`consumer.go:25-35`；raw evidence dedup 于 `pgevidence/store.go:31-60`。关键失败窗口：

| Failure Window | Retry Risk | Required Handling |
|---|---|---|
| Before CreateSession succeeds | No session expected | Retry whole Add |
| After CreateSession, before response observed | Duplicate session possible | 确定性 session_id（已验证支持）+ smoke 碰撞语义 |
| After create, before BatchAddMessages | Orphan empty session | Retry reuse session or safely abandon `(unverified)` |
| After batch accepted, before response | Duplicate messages possible | Session/message idempotency must be smoke-tested |
| After batch, before commit | Archived messages without extraction | Retry commit on same session |
| After commit accepted, before response | Duplicate extraction possible | 确定性 session_id + commit 幂等 smoke |
| After Add success marker | No memory-target retry | Async extraction may still fail |
| Async task failure | Outbox 已标 target complete | Separate task reconciliation `(unverified)` |

OV `CreateSession` 接受客户端 `session_id`（已验证，见 §Self-Host）。首推 identity 策略：

```text
stable capture identity = account + user_id + agent_id + run_id + turn_seq
   --> deterministic OV session_id (client-supplied, verified supported)
```

re-create 同 id 的精确行为（no-op / 409 / duplicate）仍需 P0 smoke——session 服务存在 exists-check（`session.py:682`）是有利信号但非保证。若 smoke 显示 re-create 非幂等，回退策略：

| Strategy | Preconditions | Trade-off |
|---|---|---|
| Client-supplied deterministic session ID | OV re-create 同 id 行为 smoke 验证 | Simplest replay model |
| Server idempotency key | A verified idempotency mechanism exists `(unverified)` | Clean HTTP semantics |
| External dedup ledger | Store capture identity + session ID + commit state transactionally | Adds state + recovery logic |
| Query-before-create (`SessionExists`) | Verified `SessionExists` Go SDK method 已存在 (`sessions.go`) | Racy unless server-enforced |
| Accept duplicates | None | Rejected for production cutover |

外部 dedup ledger（若需要）应跟踪：capture identity / OV session ID / state (created, messages accepted, commit accepted) / task ID / last error / updated ts。该 ledger 是拟议组件 `(unverified)`，仅当 OV 不能提供已验证幂等 session 流时才加。

Recovery authority 仍是 `raw_evidence`，不是 OV 派生记忆。

```text
raw_evidence (authoritative audit/replay source)
   --replay--> evidence outbox event --> OV session archive --> derived typed memories
```

OV session archive 是额外的派生/后端状态，不替代 `memory.raw_evidence`。迁移只改：

| Existing | Target |
|---|---|
| `event.Backend = "mem0"` | `event.Backend = "openviking"` |
| memory target = mem0 client | memory target = OpenViking client |
| mem0 search | OpenViking recall |
| mem0 Add | OpenViking create/batch/commit |

不变：`memory.raw_evidence` schema / dedup key / evidence 目标 / Redis Stream / DLQ / per-target marker / eino recall 失败行为 / `pkg/memory.Backend`。Async 抽取在 commit 接受后留一个 recovery gap，切流前需运维 reconciliation：(1) 捕获 commit task ID；(2) 验证 `GetTask` 暴露足够终态语义；(3) 定义 poller/audit/on-demand reconciliation 路径 `(unverified)`；(4) 对失败或无限 pending 抽取告警；(5) 仅经 verified 安全操作重驱动抽取；(6) 永不仅因 Phase 1 归档成功就把 raw evidence 标为已消费。本计划不含对 mem0 / pgvector / raw evidence / OV session 的任何破坏性清理。

## Migration Plan

迁移分四阶段，每阶段有显式回滚边界与完成 gate。

| Phase | Goal | Primary Output | Completion Criteria |
|---|---|---|---|
| P0 | Stand up OV self-host | Reachable + authenticated server + recall smoke | Health, tenant isolation, write/commit/task, extraction, recall verified |
| P1 | Implement Backend adapter | `internal/memory/openviking/client.go` | Contract + negative-path tests pass |
| P2 | Wire config + dual-run | Config-selectable backend with shadow comparison | No request-path regression; parity evidence collected |
| P3 | Cut over production | OV primary, raw evidence label updated | memory-v1 §8 + §9 gates pass |

### P0 Deliverables

P0 部署 OV，不改 eino。

| Work Item | Design |
|---|---|
| Deployment form | Docker local/staging; Helm production |
| Server port | 1933 |
| Health probe | `GET /health` |
| Worker count | 1 |
| State path | Persistent `/app/.openviking` |
| Configuration | JSON `ov.conf` |
| Account | Stable `"xkong"` |
| Users | Tenant-scoped OV users/keys |
| Runtime key | User key, not root key |
| Vector backend | `opengauss` or `local` after probe |
| Embedder | Ollama `qwen3-embedding:0.6b` (1024 dimensions) |
| Collection | Fresh OV collection |
| Network policy | Allow agent-service → OV :1933 |
| Existing mem0 | Keep running |

P0 成功需真实端到端证据：(1) `GET /health` 返回预期 payload；(2) tenant user 可建 session；(3) 可 batch 加 message；(4) commit 返回已接受 task ID；(5) task 到达 verified 成功终态；(6) recall 返回语义相关内容；(7) 另一 user 的 recall 看不到前者内容；(8) `peer_scope:"actor"` 排除其他 actor；(9) 所选 embedder 产出配置维度的向量；(10) server restart 保已 commit 状态；(11) 非法/未授权 key 被拒；(12) 失败抽取可观测（失败注入机制 `(unverified)`）；(13) re-create 同 `session_id` 碰撞语义 smoke（no-op/409/duplicate）。

### P1 Deliverables

P1 新增 `xkong-agent-center/internal/memory/openviking/client.go`，结构镜像 mem0 client 但保留 OV 语义。

| Component | Responsibility |
|---|---|
| Client config | Base URL, API key, account ID, timeout, recall settings |
| HTTP transport | Context-aware requests, bounded timeout |
| Headers | API key, account, user, actor peer |
| Search | Direct `/api/v1/search/recall` |
| Add | CreateSession(deterministic id) → BatchAddMessages → CommitSession |
| Response validation | Reject HTTP success with invalid semantic payload |
| Error type | Preserve status/body context without secrets |
| Retry classification | timeout, 5xx, 429, task-related |
| Interface assertion | Compile-time `pkg/memory.Backend` |
| Logs | Correlation fields, no message contents by default |
| Tests | Mock HTTP only; no real DB writes |

文件布局：

```text
xkong-agent-center/internal/memory/
  mem0/client.go
  openviking/client.go
  openviking/client_test.go
```

P1 测试覆盖：Search 映射 content+score / 回退 summary / 空 content+summary 跳过 / 超 K 截断 / 低于阈值排除 / 畸形成功 payload 报错 / 401 permanent / timeout temporary / 5xx temporary / 429 显式分类 / Add 调用序 create→batch→commit / create 失败不调 batch+commit / batch 失败不调 commit / commit 失败 Add 返错 / commit 接受 Add 不等 task 完成 / tenant headers 正确 / actor header 正确 / 空 UserID 不触 backend（经 service）/ message 保序 / 敏感值不入错误日志 / 模糊 commit 后重试幂等策略。

### P2 Deliverables

P2 引入配置化 backend 选择。拟议配置（占位名 `(unverified)`，实现时遵循 xkong 命名）：

| Setting | Purpose |
|---|---|
| `MEMORY_BACKEND` | `mem0` or `openviking` |
| `OPENVIKING_URL` | Server base URL |
| `OPENVIKING_API_KEY` | Tenant-scoped key |
| `OPENVIKING_ACCOUNT` | Stable account ID |
| `OPENVIKING_TIMEOUT_MS` | Add/Search HTTP timeout |
| `OPENVIKING_RECALL_MIN_SCORE` | Score threshold |
| `OPENVIKING_RECALL_PEER_SCOPE` | actor or all |
| `MEMORY_SHADOW_BACKEND` | Optional dual-run target `(unverified)` |

Wiring 改动在 `eino_wiring.go:96-100` 与 `conf.go:209`、`:217-225` 附近。Dual-run 不得过早改变用户可见行为：

| Operation | Primary | Shadow | Comparison |
|---|---|---|---|
| Search | mem0 | OV | Card presence, relevance, latency, errors |
| Add | Existing outbox target | Optional OV shadow target `(unverified)` | Acceptance, extraction completion, duplication |
| User response | mem0 result only | Never injected | No behavioral change |
| Audit evidence | raw_evidence | Same source | Replay parity |

Shadow 写不得默默改变双目标成功标记模型——若加第三 fan-out 目标会改 durable accounting，用独立 replay/shadow worker `(unverified)`。P2 完成判据：backend 选择可逆 / mem0 仍主 / OV Search shadow 不致请求失败 / OV Add shadow 不改 mem0 成功标记 / 跨租户+actor 探针干净 / Search p50/p95 与错误率记录 / 抽取接受→召回延迟记录 / 重复抽取率度量 / 代表性 raw evidence replay 产出可召回 OV 记忆 / 运维面板默认不含消息原文 `(unverified)`。

### P3 Deliverables

P3 把 OV 设为选定 backend。

| Cutover Change | Required Action |
|---|---|
| eino backend | Select OpenViking |
| outbox memory target | Use OV client |
| event backend label | `"mem0"` → `"openviking"` |
| raw evidence target | No change |
| recall path | OV Recall |
| mem0 service | Retain during rollback window |
| mem0 data | Do not delete |
| pgvector data | Do not delete |
| rollback | Config switch back to mem0 |

切流序：冻结接受基线 → 验证 OV 服务与 tenant key → 启用 OV 写 → 确认抽取与召回 → 切 Search 主 → 观察 exit-gate 窗 → pass 则保留回滚数据、计划后续退役；fail 则切回 mem0。终态 exit gate = 现有 memory-v1 checklist：

| Gate Source | Required Proof |
|---|---|
| `memory-v1.md §8` | Negative-path suite passes |
| `memory-v1.md §8` | Cross-tenant isolation passes |
| `memory-v1.md §8` | `model_emitted_user_id_blocked` passes |
| `memory-v1.md §9` | Search p95 within budget |
| `memory-v1.md §9` | raw_evidence replay achieves recall parity |
| This plan | Async extraction freshness acceptable |
| This plan | Idempotent retry behavior proven |
| This plan | Rollback operational |

见 `agent-memories/docs/design/memory-v1.md §8-§9`。「Parity」不指 mem0 与 OV 文本逐字节相同——指同一 authoritative evidence 对已验证 eino 工作流产出足够相关、租户正确的召回。精确评估 rubric `(unverified)`，须在 dual-run 评分前固定。

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| API-shape mismatch | One Add = three network calls with more partial-failure states | Encapsulate sequence in backend; test every boundary; resumable/dedup retries via 确定性 session_id |
| Async extraction latency | Newly captured facts may not appear on next request | Return after commit acceptance; measure acceptance-to-recall delay; set freshness budget `(unverified)` |
| Async task failure after Add success | Outbox marks target success while extraction later fails | Persist task correlation + reconciliation `(unverified)` |
| No Milvus adapter | Vector infra assumptions may not transfer | Choose verified `opengauss` or `local`; do not wait for Milvus |
| `opengauss` compatibility | Exact reuse of current PG instance may fail | Staging compatibility probe; fall back to `local` |
| Local backend scaling | Embedded storage + PID lock complicate multi-worker | Start one worker; validate backup/restart; avoid lock bypass |
| Embedding dim change | 1024-dim vectors cannot share new collection | Always fresh OV collection |
| Embedding configuration drift | Provider preprocessing or model revisions may change ranking despite the same model name | Pin provider/model/dimension; run replay parity before cutover |
| Later embedder switch | Vectors incompatible | Treat any model/dim switch as full re-index |
| Session re-create collision | At-least-once retries may duplicate sessions/extractions | 确定性 session_id 已验证支持；P0 smoke 碰撞语义；必要时外部 dedup ledger |
| Partial Add failure | Orphan sessions / duplicate messages | Record session state; resume by verified session ID |
| Recall content mapping | `content` verbose / `summary` loses detail | Compare both during dual-run; freeze deterministic mapping |
| Score calibration | OV scores may not align with mem0 | Treat as informational; calibrate `min_score` via replay |
| Quota mismatch | Per-type quotas return different distribution than K | Define + test quota allocator; cap final at K |
| Actor peer misconfig | Cross-agent memories may leak or disappear | Explicit actor header + `peer_scope:"actor"`; isolation tests |
| Tenant header omission | Data falls into default tenant | Require explicit account+user headers; fail closed when absent |
| Root key misuse | Runtime compromise gains management authority | Use tenant-scoped user keys; root key provisioning only |
| Prior rejection valid for old scope | Broader platform may add ops burden | Keep two-method seam narrow; require measurable consolidation/recall benefit |
| Broader OV surface | More endpoints + state = security/ops burden | Network-restrict service; scoped keys; monitor only required endpoints |
| R9-style upstream risk | Upstream OV change may alter session/recall/SDK `(unverified)` | Pin tested version/commit; freeze wire fixtures; validate upgrades |
| Go SDK gap | Recall needs custom HTTP code | Implement only verified Recall request; consider upstreaming later |
| Shadow-write accounting | Extra target may corrupt outbox completion | Keep shadow replay separate unless success-marker model explicitly extended |
| raw_evidence confusion | Operators treat OV archive as audit source | Document raw_evidence as authoritative replay input |
| Rollback divergence | OV-only memories absent after rollback | Retain raw evidence; provide replay into mem0 `(unverified)` |
| Network policy drift | Agent can't reach :1933, or excess workloads gain access | Replace mem0-specific allow rule with least-privilege OV access |
| Payload-semantic false positives | HTTP 200 may contain unusable recall data | Validate actual RecallEntry fields, not status/body presence |
| Sensitive logging | Messages/memory/keys may leak | Log identifiers/statuses only; redact secrets + content |
| Service restart loss | Bad volume config may lose embedded state | Persist `/app/.openviking`; verify restart recovery before cutover |

## Open Questions

| Question | Why It Blocks | Required Evidence | Owner |
|---|---|---|---|
| re-create 同 `session_id` 的碰撞语义（no-op/409/duplicate）？ | 决定 Add 重试是否幂等 | 重复 POST /sessions 同 id 集成测试 | OV/backend |
| BatchAddMessages 重试在同 session 内幂等吗？ | 模糊响应可能复制消息 | 重复同 batch 检查归档/抽取结果 | OV/backend |
| CommitSession 在已 commit 的 session 上幂等吗？ | 超时后重试可能复制抽取 | 重复 commit 检查 task/session 结果 | OV/backend |
| 失败 async 抽取 task 在 GetTask 如何表示？ | Add 在抽取完成前返回 | 含注入失败的终态 smoke | OV/operations |
| 首部署用 local GGUF embedder？ | 简化拓扑但改模型/维度 | Replay parity + 容量测试 | ML/ops |
| 向量存储用 `opengauss` 还是 `local`？ | 改运维/扩展/recovery 模型 | Staging 负载/重启/备份/兼容证据 | Storage/ops |
| `opengauss` 能安全用现有 PG 实例吗？ | 决定能否合并基础设施 | 版本/扩展/schema/隔离 probe | DBA/ops |
| OV 共享 DB 实例但用独立 db/schema 凭证？ | 共享故障域可能大于合并收益 | 容量+隔离评审 `(unverified)` | DBA/security |
| RecallEntry `content` 还是 `summary` 填 MemoryCard？ | 影响 prompt 大小与召回保真 | Dual-run 相关性评估 | Agent/ML |
| content 与 summary 同时存在时如何选？ | 映射须确定性 | 代表性响应语料 | Agent/ML |
| K 如何映射 per-type quotas？ | Backend 单限 / Recall typed quotas | Replay benchmark across K=1,5,20 | Agent/ML |
| 何 `min_score` 匹配 eino 召回？ | 默认 0.1 可能不匹配 mem0 | 标注 replay 校准 | Agent/ML |
| `peer_scope` 总是 `"actor"`？ | `"all"` 可能共享但风险泄漏 | 产品策略+隔离测试 | Product/security |
| 空 AgentID 如何处理？ | Actor scoping 可能未定义 | 契约决策+负路径测试 | Backend |
| `reason: "eino-turn"` 如何保留？ | OV session API 未验证 reason 字段 | 源码检查；不支持则省略 | Backend |
| 可接受抽取 freshness 预算？ | 决定 fire-and-forget commit 可行性 | 类产延迟分布 | Product/ops |
| 切流前是否需 async task reconciliation？ | 否则失败抽取可能静默 | 失败率+task 状态证据 | ops |
| mem0 保留多久用于回滚？ | 控运维成本与恢复信心 | Exit-gate 观察策略 `(unverified)` | ops |
| raw_evidence replay 如何定向到单一后端？ | parity 与回滚需要 | 现有 replay 工具检查 `(unverified)` | Data/backend |
| 何谓 recall parity？ | 跨引擎文本非逐字节相同 | 固定标注评估 rubric `(unverified)` | Product/ML |
| OV 元数据以后是否暴露？ | `type`/`origin`/uri 当前丢弃 | 独立接口演进提案 | Architecture |
| pin 哪个 OV 版本/commit？ | 上游可能漂移 | Release 选择+冻结 smoke fixture | Release/ops |
| Recall 是否加入上游 Go SDK？ | 去除本地 raw HTTP 但扩 scope | 独立上游提案 | OV |
| 429 重试策略？ | mem0 当 permanent，OV 行为未验证 | 受控响应测试+运维策略 | Backend |
| 认证 rotation 流程？ | Tenant key 须轮换不掉捕获 | 双 key 能力检查 `(unverified)` | Security/ops |

## Source Map

| Claim | Source |
|---|---|
| `Memory` exposes Search and Add | `xkong-agent-center/pkg/memory/memory.go:60-72` |
| `Backend` is the two-method pluggable seam | `xkong-agent-center/pkg/memory/memory.go:79-82` |
| Scope contains UserID, AgentID, RunID | `xkong-agent-center/pkg/memory/memory.go:28-32` |
| Message contains Role and Content | `xkong-agent-center/pkg/memory/memory.go:38-41` |
| MemoryCard contains Memory and Score | `xkong-agent-center/pkg/memory/memory.go:49-52` |
| K / query / message / content limits | `xkong-agent-center/pkg/memory/policy.go:6-15` |
| Service constructed with `NewService(backend Backend)` | `xkong-agent-center/pkg/memory/service.go:33` |
| Missing UserID prevents backend access | `xkong-agent-center/pkg/memory/service.go` |
| mem0 client implements Backend | `xkong-agent-center/internal/memory/mem0/client.go:218` |
| mem0 Search posts to `/search` | `xkong-agent-center/internal/memory/mem0/client.go:115-133` |
| mem0 Add posts inferred memories | `xkong-agent-center/internal/memory/mem0/client.go:156-168` |
| mem0 uses `X-API-Key` | `xkong-agent-center/internal/memory/mem0/client.go:36` |
| mem0 temporary/permanent error classification | `xkong-agent-center/internal/memory/mem0/client.go:48-49` |
| mem0 timeout and status handling | `xkong-agent-center/internal/memory/mem0/client.go:60-62` |
| mem0 client wiring | `xkong-agent-center/internal/server/eino_wiring.go:96-100` |
| MemoryConfig fields | `xkong-agent-center/internal/conf/conf.go:217-225` |
| Existing memory env configuration | `xkong-agent-center/internal/conf/conf.go:209` |
| mem0 self-host launcher | `agent-memories/scripts/mem0-up.sh` |
| mem0 Docker services and model configuration | `agent-memories/vendor/mem0/server/docker-compose.yaml` |
| Current committed Kubernetes network policy | `xkong-agent-center/deploy/k8s/networkpolicy.yaml` |
| eino holds `pkgmemory.Memory` | `xkong-agent-center/internal/backend/eino/backend.go:46` |
| eino recall scope construction | `xkong-agent-center/internal/backend/eino/backend.go:302-306` |
| Cross-session recall clears RunID | `xkong-agent-center/internal/backend/eino/backend.go:319-320` |
| eino calls Search and tolerates recall failure | `xkong-agent-center/internal/backend/eino/backend.go:321` |
| Durable and inline capture paths | `xkong-agent-center/internal/backend/eino/backend.go:508-531` |
| Durable event includes reason and backend | `xkong-agent-center/internal/backend/eino/backend.go:516-518` |
| Legacy inline Add | `xkong-agent-center/internal/backend/eino/backend.go:523-527` |
| Evidence stream and DLQ names | `xkong-agent-center/internal/memory/evidenceoutbox/outbox.go:21-22` |
| Consumer has evidence and memory targets | `xkong-agent-center/internal/memory/evidenceoutbox/consumer.go:25-35` |
| Outbox consumer wiring | `xkong-agent-center/internal/server/evidence_outbox_wiring.go:77` |
| raw_evidence schema and dedup | `xkong-agent-center/internal/memory/pgevidence/store.go:31-60` |
| Previous OpenViking rejection | `agent-memories/docs/investigations/agent-memories-build-vs-adopt.md §4` |
| Existing graph-shape concern | `agent-memories/docs/design/memory-v1.md:148` |
| Backend-switch negative and performance gates | `agent-memories/docs/design/memory-v1.md §8-§9` |
| OpenViking CLI server entry | `openviking_cli/server_bootstrap.py:59` |
| OpenViking server bootstrap | `openviking/server/bootstrap.py:126` |
| Server defaults | `openviking/server/config.py:268-272` |
| Configuration precedence | `openviking/server/config.py:356-378` |
| Local workspace PID-lock setting | `openviking_cli/utils/config/storage_config.py:26` |
| Server router registration | `openviking/server/app.py:526-565` |
| Health endpoint | `openviking/server/routers/system.py:70` |
| CreateSession endpoint + session_id field | `openviking/server/routers/sessions.py:129`, `:188` |
| CommitSession and async extraction | `openviking/server/routers/sessions.py:426` |
| Extract endpoint | `openviking/server/routers/sessions.py:465` |
| AddMessage endpoint | `openviking/server/routers/sessions.py:476` |
| BatchAddMessages endpoint | `openviking/server/routers/sessions.py:532` |
| Find endpoint | `openviking/server/routers/search.py:176` |
| Search endpoint | `openviking/server/routers/search.py:218` |
| Recall endpoint | `openviking/server/routers/search.py:269` |
| RecallRequest fields | `openviking/server/routers/search.py:142-154` |
| RecallEntry fields and origins | `openviking/retrieve/type_quota_recall.py:42-70` |
| Recall type order and defaults | `openviking/retrieve/type_quota_recall.py` |
| OpenViking memory categories and paths | `docs/en/api/16-memory.md:6-21` |
| Experimental `ov add-memory` composition | `crates/ov_cli/src/commands/session.rs:358-398` |
| Session exists-check | `openviking/session/session.py:682` |
| Go SDK Client and NewClient | `sdk/go/client.go:14`, `:27` |
| Go SDK Find / Search | `sdk/go/retrieval.go:9`, `:47` |
| Go SDK CreateSession (accepts SessionID) | `sdk/go/sessions.go:12` |
| Go SDK AddMessage / BatchAddMessages | `sdk/go/sessions.go:80`, `:98` |
| Go SDK CommitSession / GetTask | `sdk/go/sessions.go:109`, `:123` |
| Go SDK SessionExists | `sdk/go/sessions.go` |
| Verified vector backend list | `openviking/storage/vectordb_adapters/factory.py:17-25` |
| Default vector backend is local | `openviking_cli/utils/config/vectordb_config.py` |
| Embedding providers | `openviking_cli/utils/config/embedding_config.py` |
| Local GGUF embedder and default model | `openviking/models/embedder/local_embedders.py` |
| Server quickstart | `docs/en/getting-started/03-quickstart-server.md` |
| Standalone embedded deployment | `docs/en/guides/03-deployment.md:72` |
| systemd deployment example | `docs/en/guides/03-deployment.md:90` |
| Account/user/actor identity mapping | `openviking/server/identity.py` |
| API-key and Bearer authentication | `openviking/server/auth/__init__.py` |
| Docker image, port, health, state path | `Dockerfile` |
| Docker Compose deployment | `docker-compose.yml` |
| Helm deployment artifact | `deploy/helm/openviking/` |
| Build-only Makefile surface | `Makefile` |
