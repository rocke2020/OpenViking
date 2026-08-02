# VikingBot Agent 设计解析与 Eino 演进建议

> **一句话结论**：Eino 不应复制 VikingBot 的 Python 实现，而应吸收它的分层思想——用 Skill 管“如何做”，用 Tool 管“执行什么”，再为 Eino 建立能在执行期管理“本轮允许做什么”的 ToolRegistry，并仅把高风险执行路由到真正隔离的 Sandbox。

## TL;DR

本设计建议把 Eino 保持为轻量 Agent service：保留无状态请求级运行、`internalRunner`、有序 `RunEvent`、结构化 Action 与 durable outbox；skill-service 提供外置 Skill，OpenViking 逐步替换 mem0 成为外置长期记忆。Eino 只新增带执行期授权的 ToolRegistry，并按风险选择 Sandbox；不照搬 VikingBot 的常驻工作台形态及其当前实现 caveat。

建议直接采用以下七个决策：

1. **保留 Eino 主链**：继续使用请求级 `adkRunner`、`internalRunner` 边界、单一有序 `RunEvent`、`EinoEmitter`、`ActionProcessor` 和 durable outbox（先持久化事件、再异步交付）。
2. **明确 Tool/Skill 双层**：Tool 是最小可执行能力；Skill 是可版本化、可按需加载、可组合 Tool 的任务规程。不要再把 Skill 永久压扁成一个 HTTP Tool 或一段常驻 system prompt。
3. **分开目录与闸门**：CapabilityCatalog 负责发现 Skill/Tool；ToolRegistry 负责构造本轮 ToolSet 并执行。目录里“存在”不代表本轮“可见”，模型“看见”也不代表执行时“获准”。
4. **执行期再次授权**：每个调用都根据 actor、channel、tenant、risk、side effect、Sandbox profile 和本轮快照校验；不能只依赖发送给模型前过滤 tool schema。
5. **按风险使用 Sandbox**：业务查询 API 留在进程内；文件、Shell、代码、用户上传物、在本地启动的不可信 MCP 等进入隔离后端。大陆 Eino 仍可保持无状态，Sandbox 只在需要时按 session 临时创建。
6. **外置长期记忆**：复用 `pkg/memory` seam，让 OV adapter 接管 Search/异步 Add；Redis History 继续负责短期逐字上下文，不能把 OV client 类型渗入 ADK loop。
7. **保留结构化结果**：Tool 返回 typed result/error/evidence，Action 继续走结构化协议；不要退回“模型输出文本标签再解析”的路径。

最短决策表：

| 能力 | 应用形态 | 例子 |
|---|---|---|
| 一次强类型调用即可完成 | Tool | 查客户、查天气、搜索资源目录 |
| 有分支、顺序、重试、确认或验收规则 | Skill + Tool | 日历改期、报表生成、部署检查 |
| 固定表达/安全规则，不需要外部动作 | Prompt policy | 回复语言、禁止编造来源 |
| 必须确定性、可审计地按固定状态推进 | Workflow engine + Tool | 支付、审批、医疗硬状态机 |
| 会接触本地文件、Shell、代码或不可信扩展 | Skill/Tool + Sandbox | CSV 分析、代码执行、本地 MCP 进程 |

## 1. 结论边界与阅读方式

本文是基于当前源码快照的设计学习笔记，不是对 VikingBot 或 Eino 的功能宣传。结论会明确区分“设计意图”“源码现状”和“推荐目标”，因为三者在 Tool 授权、Skill 装载与 Sandbox 隔离上并不完全相同。

本文的证据范围如下：

- **VikingBot 源码**：`AgentLoop`、`ContextBuilder`、`SkillsLoader`、`Tool`、`ToolRegistry`、默认 Tool 工厂、MCP、Subagent、SandboxManager 与四个 Sandbox backend。
- **Eino 设计**：`xkong-agent-center/docs/design/通用agent双轨架构/main.md`，重点是 D1/D4/D5/D8/D9/D10、无状态成本模型、Skill 二分类、结构化 Action 与 durable outbox。
- **Eino 当前实现**：`adk_runner.go`、`runner.go`、`snapshot.go`、`snapshot_provider.go`、`tool_skill_service.go`、`eino_wiring.go`。实现已经比设计稿中的早期描述更完整，因此比较以源码为准、设计稿解释意图。
- **验证边界**：本文做静态源码审阅，没有启动 Bot、连接 skill-service 或实测各 Sandbox backend；部署是否启用 Langfuse、skill-service、mem0 或某个 Sandbox，仍取决于运行配置。

本文使用三个不同概念，不能混写：

| 概念 | 本文含义 |
|---|---|
| OpenViking | 资源、记忆、Skill 等长期上下文系统 |
| VikingBot / OV Bot | 消费 OpenViking 上下文并运行 Agent loop 的 Bot runtime |
| Eino Agent | `xkong-agent-center` 内的大陆通用 Agent 轨；香港轨仍是 OpenClaw |

## 2. VikingBot Agent 的主架构

VikingBot 的核心不是某个工具集合，而是 `Context → Model → ToolRegistry → Model` 的迭代循环；Skill、Memory、Workspace 与 Sandbox 分别进入提示、知识、状态和执行边界。这个分层比具体 Python 类更值得 Eino 借鉴。

```text
Inbound Channel / HTTP
          │
          ▼
      MessageBus
          │
          ▼
      AgentLoop ─────────────── Session / History
          │
          ├── ContextBuilder ── Identity / Workspace / Memory
          │          │
          │          └──────── Skill summary + selected full SKILL.md
          │
          ▼
      LLM Provider
          │ tool_calls
          ▼
      ToolRegistry
          ├── schema / validation
          ├── trusted ToolContext
          ├── Langfuse trace + Hooks
          └── Tool.execute()
                  ├── business / Web / OpenViking / MCP
                  └── file / exec ── SandboxManager ── Backend
          │
          └──────── tool result ─────► LLM Provider ──► final reply
```

这里有四条互相独立的边界：

- **Context 边界**：`ContextBuilder` 决定模型知道什么，包括身份、会话、记忆、Workspace 和 Skill 指令。
- **Capability 边界**：Tool schema 决定模型可以请求什么，但只是一份候选能力目录。
- **Execution 边界**：ToolRegistry 与 Tool 实现决定请求是否真的执行、以谁的身份执行、如何记录结果。
- **Isolation 边界**：Sandbox 决定文件和命令能碰到哪些 OS 资源；Prompt 不能替代这条边界。

Skill 不在执行路径上“直接运行”。它通过 Prompt 告诉模型何时、按什么顺序、带什么约束去调用 Tool；真正产生外部副作用的仍然是 Tool。Subagent 也不是权限放大器：VikingBot 为其另建较小的 ToolRegistry，默认排除消息、定时、图像和 OpenViking 等能力。

## 3. 一次请求如何运行

一次 VikingBot 请求会先固定会话与工作区，再组装身份、记忆和 Skill 摘要；模型选择 Tool 后由 ToolRegistry 注入可信运行上下文并执行，结果回灌模型，直到得到最终文本或达到迭代上限。

源码路径可以压缩为九步：

1. **确定 SessionKey**：Channel、chat/session 标识共同决定历史和 workspace scope。
2. **选择 Sandbox workspace**：`SandboxManager` 把 SessionKey 映射为 shared、per-channel 或 per-session 的 workspace ID，并懒创建 backend。
3. **构建 Context**：`ContextBuilder` 注入身份、bootstrap 文件、历史、OpenViking memory、always Skill 全文，以及普通 Skill 的摘要目录。
4. **生成本轮 Tool schema**：`ToolRegistry.get_definitions()` 按 `ov_tools_enable` 和 `disabled_tools` 隐藏部分 Tool，然后转换成 OpenAI function schema。
5. **调用模型**：Provider 返回普通文本或一个/多个 tool call。
6. **执行 Tool**：解析参数后，Registry 创建 `ToolContext`，做参数校验、调用 `Tool.execute()`，并包装 Langfuse span 与 post-call Hook。
7. **并发汇合**：同一模型响应里的多个 tool call 当前用 `asyncio.gather()` 全部并发执行，再按原 call 顺序写回结果。
8. **继续推理**：结果作为 tool message 回灌，同时追加“反思结果并决定下一步”的提示，进入下一轮。
9. **结束与持久化**：得到 plain text 后结束；达到最大迭代数时，AgentLoop 会禁用 Tool 再要求模型总结，随后保存 Session 并发出 OutboundMessage。

这条链路有三个重要含义：

- **模型负责规划，Registry 不负责规划**。Registry 不理解任务目标，只验证并执行指定名称的 Tool。
- **Tool 可以动态加入**。默认 Tool 由 factory 注册，MCP server 连接后也把远程能力包装成普通 Tool，名称形如 `mcp_<server>_<tool>`。
- **Langfuse 是可选观测，不是运行依赖**。Registry 总是走统一埋点入口，但只有 `langfuse.enabled=true` 时才创建并结束 tool span；禁用时不影响 Tool 调用。

当前“所有同轮 Tool 并发”只适合彼此独立、只读、无共享资源的调用。写后读、两个文件编辑、日历先查再改、共用幂等键或同一业务对象的操作必须由调度策略串行化，不能依靠模型自然生成正确批次。

此外，主 `run()` 是单 inbound consumer；一个长请求会阻塞其他 Session。Eino 的请求级并发模型更适合中央多租户服务，不应复制这个全局队首阻塞结构。

## 4. 什么时候用 Tool，什么时候用 Skill

最实用的判断不是“单步或多步”这么简单：Tool 是可授权、可校验、可观测的原子执行边界；Skill 是告诉模型如何选择和组合这些边界的版本化操作规程。复杂能力通常同时需要 Skill 和多个 Tool。

### 4.1 Tool：原子执行契约

满足下列多数条件时，应建成 Tool：

- 需要访问模型之外的实时数据、服务、文件或设备；
- 输入可以用 JSON Schema/Go struct 明确定义；
- 调用可以独立设置鉴权、超时、限流、幂等、审计和重试；
- 结果可以用稳定结构表达，而不是靠模型阅读一大段 SOP 才能解释；
- 一次调用代表一个清楚的副作用边界。

例如 Eino 当前的 `web_search`、`web_fetch`、`search_resource_catalog`、`query_customer_info` 和“今日预约查询”都是 Tool。它们把事实返回给 ReAct loop，不需要为了“看起来像 Agent 能力”而包装成 Skill。

### 4.2 Skill：可加载的任务规程

满足下列任一条件时，应建成 Skill：

- 任务包含多步顺序、条件分支、失败恢复或 fallback；
- 需要规定何时调用哪个 Tool、何时不能调用；
- 需要领域判断、输出验收、证据要求或用户确认；
- 需要随运营规则独立版本化，并附模板、示例或资源文件；
- 完整说明较长，不值得每轮常驻 Tool description 或 system prompt。

VikingBot 的普通 Skill 先只暴露 `name + description + location`，模型选中后再用 `read_file` 读取 `SKILL.md`；`always=true` 的 Skill 才全文常驻。这个 progressive loading 是值得迁移的关键：它把“能力发现”和“完整规程加载”分成两次决策，减少无关 token。

当前有两个目录边界：摘要只扫描本地 workspace Skill；OpenViking 中持久化的 Skill 不会自动进入该列表，需要走 OpenViking 检索路径。并且缺依赖的 workspace Skill 会在摘要构造前被过滤，使后续 `available=false/requires` 提示分支不可达；这些是现状 caveat，不是目标语义。

Skill 应遵守两个执行边界：

1. **Skill instruction 不能扩权**。它不能新增本轮不可用的 Tool，也不能绕过服务端授权、hard deny 或 Sandbox；宿主是否允许可信 Skill metadata 临时预授权 Tool，属于 runtime policy。
2. **高频或高风险动作应显式建模**。在 Eino 中，这类动作不应长期隐藏在 `curl`、SQL 或 Shell 示例中，而应提升为具有鉴权、参数校验、审批和审计能力的专用 Tool。

### 4.3 同时使用 Skill 与 Tool

复杂业务通常不是二选一：

| 能力 | Skill 负责 | Tool 负责 |
|---|---|---|
| 日历改期 | 先确认对象、处理模糊时间、冲突时追问、写后核验 | 查事件、更新事件、读取真实日期 |
| 报告生成 | 数据检查、分析顺序、质量门、交付格式 | 读文件、运行分析、写产物、发布 artifact |
| 部署检查 | 环境选择、门禁、失败停止、回滚条件 | 读状态、触发部署、查日志、健康探测 |

一个 Skill 可以组合多个 Tool；一个 Tool 也可以被多个 Skill 复用。Skill 的价值在决策规则，不在重新包装执行代码。

### 4.4 不该交给 Skill 的流程

当顺序和状态转换必须百分之百确定、错误会造成资金/合规/医疗等重大风险时，应由 workflow engine 或业务状态机主导，LLM 只负责理解意图、补参数和解释结果。你现有医疗主链路继续使用硬编码 `WorkflowEngine`，正是正确边界；不应为了统一成“通用 Agent”而改造成自由 ReAct。

对 Eino 当前 D5 的修正建议是：保留“API Tool 与多步策略”分类，但把第二类正式命名为 Skill，并允许它按需加载，而不是永远等同于一段全量 system instructions。Calendar 是现有首例，不应成为永久特例。

## 5. ToolRegistry 的真实逻辑与正确边界

VikingBot 的 ToolRegistry 同时承担注册表、模型 schema 目录、参数校验入口、运行上下文注入、调用包装和观测钩子。它是 Agent 的执行总闸门雏形，但当前“可见性过滤”还不是完整的执行授权。

### 5.1 当前实现做了什么

| 阶段 | VikingBot 行为 | 设计含义 |
|---|---|---|
| 注册 | `register(tool)` 写入 `name → Tool` 字典 | Tool 名称是调用与路由主键 |
| 披露 | `get_definitions()` 过滤并输出 function schema | 控制模型本轮看见的候选能力 |
| 定位 | `execute(name, params, ...)` 按名称查找 | Registry 是统一 dispatch 入口 |
| 上下文 | 服务端构造 `ToolContext` | 身份、Session、Channel、OV connection 不应由模型提供 |
| 校验 | `tool.validate_params(params)` | 在执行前阻断明显的形状错误 |
| 执行 | `await tool.execute(context, **params)` | 具体副作用归 Tool 实现 |
| 观测 | Langfuse span、耗时、Hook | 横切逻辑不散落到每个 Tool |
| 回传 | 结果或错误转成字符串 | 交给模型决定下一步 |

默认 factory 集中注册文件、Shell、Web、OpenViking、图片、消息、spawn 和 cron；MCP 再把远程 Tool 动态注册进同一个 Registry；Subagent 则使用一个更小的 Registry。这个模式的优点是所有执行都有同一入口，而不是各业务模块绕过 Agent runtime 自己调用。

### 5.2 当前实现不能照搬的地方

1. **披露不是授权**：`disabled_tools` 和 `ov_tools_enable` 只影响 `get_definitions()`；`execute()` 不重新检查。如果 Provider 仍返回隐藏 Tool 名，Registry 仍会执行。
2. **同名静默覆盖**：`register()` 允许后注册 Tool 无提示替换旧实现，动态 MCP 或插件容易劫持名称。
3. **校验只是 JSON Schema 子集**：未知字段不会被拒绝，也没有完整支持组合 schema；业务约束仍需 Tool 自己校验。
4. **结果类型过弱**：正常结果、异常和业务失败最终都接近字符串，成功判断依赖是否以 `Error:` 开头，不利于重试、告警和长期 evidence。
5. **上下文有实现缺口**：`ToolContext.workspace_id` 的默认表达式在 dataclass 定义期计算，Registry 又未显式传入，所以源码下通常为 `None`；不能把它当成可靠的授权字段。
6. **并发没有副作用语义**：AgentLoop 对一个响应内的所有调用统一 `gather()`，Registry 没有 `parallel_safe`、resource key 或 concurrency group。
7. **Hook 链语义不统一**：异步 Hook 通过 `gather()` 调用，但返回的修改值没有像同步 Hook 那样顺序传递；不能假定 post-call 一定能改写结果。
8. **MCP 缺少请求身份闭环**：wrapper 不使用 `ToolContext`，actor/tenant 不会自动传播；单 server 连接异常还可能被内部吞掉，使外层误判已连接。

### 5.3 Eino 应采用的 Registry 定义

Eino 不需要复制 Python 类，但需要建立等价的执行总闸门。下面是表达边界的 Go 风格伪代码，不是可直接编译的实现；重点是把“全局已安装能力”和“本轮允许能力”分开：

```go
type ToolSpec struct {
    Name            string
    Version         string
    Risk            RiskLevel
    SideEffect      SideEffect
    ParallelSafe    bool
    Idempotent      bool // 重复调用不重复产生副作用
    Timeout         time.Duration
    SandboxProfile  string
    RequiredScopes  []string
}

type RunToolSet interface {
    Definitions() []schema.ToolInfo
    Execute(ctx context.Context, call ToolCall) ToolResult
}
```

`RunToolSet` 必须由服务端 actor/tenant/channel、会话固定 catalog、kill switch（紧急停用开关）、审批状态和 Sandbox 能力共同派生，并保持本轮不可变。`Execute()` 再检查 Tool 是否属于该快照和 scope；这是比“只给模型少看几个 schema”更可靠的授权闭环。

`ToolResult` 至少应包含 `status`、typed `data`、`error_code`、`retryable`、`artifacts`、`duration`、`evidence_summary`。模型看到的是经过大小和敏感信息控制的摘要；完整结果进入 trace/evidence，但不能把密钥或大对象原样塞回 Prompt。

## 6. 为什么高风险 Agent 必须选择 Sandbox

只要模型能操作通用文件、Shell、下载内容、在本地启动不可信 MCP 或运行用户脚本，Sandbox 就不再是部署优化，而是阻止提示注入升级为宿主机权限的安全边界。纯服务端、强类型、无本地执行的业务 API Tool 则不必为形式统一而进入 Sandbox。

“必须选择 Sandbox 方案”的准确含义是：系统必须显式选择执行姿态，不能把“有一个 workspace 目录”误当成安全策略。

| 姿态 | 含义 | 适用场景 |
|---|---|---|
| `NoLocalExec` | 根本不注册通用文件、Shell、脚本、浏览器 Tool | 当前大陆 Eino 的纯业务 API Agent |
| `TrustedDirect` | 明确允许以 Agent 进程权限执行 | 单用户本机开发、可信运维任务 |
| `IsolatedExec` | 命令和文件进入容器/microVM/托管 Sandbox | 多租户、用户上传、代码执行、本地不可信扩展 |

高风险调用如果没有强隔离，会把以下问题直接放大为宿主机问题：

- **Prompt injection**：网页、文件或 MCP 返回内容诱导模型读取密钥、执行命令或外传数据；
- **路径与进程逃逸**：`../`、符号链接、绝对路径、子进程和解释器可以越过“请只在目录内工作”的文本要求；
- **网络攻击**：访问 localhost、内网服务、云 metadata 或把数据发送到任意域名；
- **租户串扰**：shared workspace、进程环境和缓存让一个会话看到另一个会话的文件；
- **资源耗尽**：死循环、fork、巨量输出、磁盘填满、CPU/内存抢占；
- **供应链扩权**：动态 Skill、CLI、包管理器和 MCP server 带来新的可执行代码与凭证面。

Prompt、Skill、JSON Schema、Tool description 都是“软约束”；Sandbox 的文件、网络、进程和资源策略才是模型无法通过更换文本绕过的“硬约束”。但 Sandbox 也不是业务授权：它不能阻止一个已获网络权限的日历 Tool 删除错误事件，也不能替代租户校验、幂等键、HITL（human-in-the-loop，人工确认）和审计。

生产 `IsolatedExec` 至少应满足：

- server-derived tenant/user/session/run ID；外部 ID 先规范化或哈希；
- 临时、最小挂载的文件系统，根目录只读，产物显式 promotion 到对象存储；
- 不继承 agent-center/skill-service 的完整环境变量，凭证按调用短期注入；
- egress（出站网络）默认拒绝，按 Tool/Skill 域名 allowlist 放行，禁止 localhost、私网和 metadata；
- wall-clock、CPU、内存、PID、磁盘、输出大小和并发上限；
- timeout 后杀完整进程树，启动失败 fail closed（失败时拒绝执行），绝不静默回退到 Direct；
- 审计记录 Tool、actor、策略版本、Sandbox profile、输入摘要、产物和终态。

## 7. VikingBot Sandbox 的后端与源码 caveat

VikingBot 已把工作区选择与后端实现解耦，支持 shared、per-channel、per-session 三种作用域以及四类后端；但“叫 Sandbox”不等于已经强隔离，尤其 `direct` 明确是在宿主执行。

### 7.1 作用域选择

| mode | workspace 映射 | 判断 |
|---|---|---|
| `shared` | 所有会话共用 `shared` | 只适合可信单用户；公共 Bot 不应使用 |
| `per-channel` | 同一 Channel 标识共用 | 适合频道级协作，但不是用户隔离 |
| `per-session` | 每个 SessionKey 独立 | 多租户默认选择；成本与清理压力更高 |

`SandboxManager` 按上述 workspace ID 懒创建并缓存 backend，也负责复制 bootstrap 和 Skill 文件。这个接口分层值得保留：Agent/Tool 不应知道本地容器、远程 Pod 或托管 Sandbox 的具体 SDK。

### 7.2 后端判断

| backend | 源码行为 | 适用判断 |
|---|---|---|
| `direct` | `asyncio.create_subprocess_shell` 在宿主执行，workspace 仅是 cwd | 本地可信开发；不是强隔离 |
| `srt` | 通过 Node wrapper 使用 sandbox-runtime 的文件/网络策略 | 轻量本地隔离方向可取，但当前接线需修复和实测 |
| `opensandbox` | 通过 OpenSandbox server 创建隔离环境 | 最接近 Eino 的生产远程执行形态 |
| `aiosandbox` | 通过远程 SDK 执行命令和文件操作 | 可用性取决于服务端身份与隔离契约 |

### 7.3 当前源码 caveat

这些是“学习时要修正”的实现事实，不代表 Sandbox 抽象本身无价值：

1. `direct` Shell 继承 Bot 进程权限和环境；workspace 文件检查不限制 Shell 读取宿主路径或访问网络。
2. `ContextBuilder` 只要存在 SandboxManager 就声称文件与命令被限制在 sandbox 目录，这对 `direct` Shell 并不成立。
3. `restrict_to_workspace` 出现在配置模型中，但 Direct Shell 没有使用它；文件 API 的路径检查和命令执行不是同一安全边界。
4. SandboxManager 捕获 `start()` 异常后仍返回实例，可能造成“Prompt 声称可用、执行时才报错”；生产实现必须 fail closed。
5. SRT 当前构造链把字符串 workspace ID 当成带 `safe_name()` 的 SessionKey，并从错误配置层读取 network/filesystem，不能仅凭“后端已注册”推断可用。
6. OpenSandbox schema 有 network、CPU、memory 字段，但 adapter 创建请求未完整传入；VKE volume 映射也未体现每 workspace 子目录。
7. AIO adapter 只用 base URL 创建 client，没有把 session/workspace identity 传给服务端，`per-session` 隔离无法由该层证明。
8. Sandbox 网络策略只覆盖经 backend 执行的操作；VikingBot 的 Web/Provider 请求有独立宿主网络路径，仍需 SSRF 与 egress 策略。
9. MCP stdio server 由 MCP client 在 Bot 侧直接启动，不经过 SandboxManager；切换 Sandbox backend 不会自动隔离它。HTTP MCP 是否需要 OS Sandbox 则取决于远端信任边界，不能一概视为 T2。
10. `readonly` mode 只少注册一个 OpenViking 写 Tool，本地 `write_file`、`edit_file` 和 `exec` 仍存在；它不是完整的文件/Shell 只读策略。

因此，Eino 若做 POC，应把 OV backend 看作接口参考，而不是现成生产认证。优先做远程容器型 `ExecutionEnvironment`，以 contract test 证明隔离，再注册 file/exec/code Tool。

## 8. 你的 Eino Agent 当前已经做对了什么

当前 Eino 轨不是空白：它已经具备无状态 ADK/ReAct、静态内部 Tool、动态 skill-service 快照、会话固定 catalog、结构化 Action、统一行为处理和 durable memory outbox。演进重点应是补齐能力治理与高风险执行面，而不是推倒重写。

### 8.1 当前真实结构

```text
buildEinoBackend
  ├── static []tool.BaseTool
  │     web_search / web_fetch / resource catalog / business queries
  ├── SnapshotProvider
  │     skill-service descriptors → fingerprint → per-RunID pin
  │                                ├── API Skill → skillServiceTool
  │                                ├── Strategy Skill → system instructions
  │                                └── advertised action_type set
  └── adkRunner.Run
        static tools + pinned snapshot tools → Eino ADK ToolNode
        ordered AgentEvent → RunEvent → EinoEmitter → ActionProcessor / SSE
```

`SnapshotProvider` 已经是很好的“会话能力快照”雏形：

- discovery 得到的 descriptor 集合按内容 fingerprint 去重；
- 在单个 `SnapshotProvider` 进程且 pin 未被 LRU 淘汰时，同一 RunID 固定到同一 snapshot；多副本下还需把 fingerprint 放进 durable session/request，才能真正避免会话中途漂移；
- 一个 snapshot 同时派生动态 Tool、Strategy prompt 和 action allow-set，减少三者漂移；
- 有 TTL 合并刷新、last-known-good 和可选持久化恢复；
- skill-service 冷启动不可用且没有 last-known-good 时，降级为 base prompt + 静态 Tool；已有快照只能保住目录，handler 服务仍不可调用，必须另有健康门与熔断。

Eino 当前实际上有三类能力：

| 类别 | 当前承载 | 评价 |
|---|---|---|
| 静态内部 Tool | agent-center Go wiring | 适合稳定、高频、强类型的基础与业务查询 |
| API Skill | descriptor → `skillServiceTool` → Python handler | 兼顾热更新，但执行治理跨 Go/Python 分散 |
| Strategy Skill | snapshot 中全文拼入 system prompt | 保留策略价值，但还不是按需 progressive loading |

部署配置仍是另一层事实：代码具备 skill-service、mem0、Langfuse 等路径，不等于某环境已启用。设计稿顶部明确记录过 config-only/nil fallback 阶段，因此运行态必须单独探测，本文不把代码存在写成“生产已开启”。

当前 Memory seam 是 provider 替换的良好起点：`EinoBackend.Chat` 只依赖 `pkg/memory.Memory.Search/Add`；Search 失败时无记忆降级，Add 由 outbox consumer 后台调用。但 `Scope` 还缺 tenant/account，`Add` 也没有稳定 event ID；接 OV 前必须做版本化扩展，不能声称原接口已完整满足多租户与幂等重试。

### 8.2 中央多租户服务是不可破坏的约束

OpenClaw 的“一用户一实例”天然把进程、workspace、凭证、Skill 和记忆绑定到 owner；中央 Eino 由许多用户共享进程与连接池，必须把这些隐式边界全部显式化：

| 边界 | 单用户 OpenClaw | 中央 Eino service |
|---|---|---|
| 身份 | 实例 owner 隐式确定 | 每请求携带服务端认证的 tenant/user/session/run |
| Tool/Skill | 本地安装集可长期存在 | 共享 catalog，派生不可变的会话/请求快照 |
| 记忆 | 实例本地或 owner workspace | OV 外置存储，所有 Search/Add 强制 scope |
| 文件执行 | owner workspace | 默认无 workspace；T2 才创建远程临时 Sandbox |
| 凭证 | 可放实例环境 | 短期、最小 scope 注入，不能让用户间共享 |
| 资源控制 | 单用户预算 | per-tenant/user 配额、并发、公平调度和熔断 |

因此 Registry、OV adapter、skill-service client 和 Sandbox pool 都不能把“当前用户”存在全局可变字段中；LLM 参数也不能携带可伪造的 user/tenant。agent-center 还应向 skill-service 签发短期、服务端派生的 tenant/user/scope delegation，并由 handler 强制校验；mTLS 只认证服务，不能替代行级 scope。即使复用远程 Sandbox，也必须有可证明的清理和重新绑定协议，否则应 per-run 创建。

### 8.3 应保留的设计资产

- **无状态成本模型**：每次 `Run` 新建 Agent/Runner，成本随并发而非用户常驻实例数增长；
- **`internalRunner` 隔离 Eino ADK**：上游变更集中在 `adk_runner.go`，业务 backend 不泄漏 ADK 类型；
- **单一有序 `RunEvent`**：模型 token 与 Tool result 不走两个需要人工合流的通道；
- **结构化 Action**：EinoEmitter 直接消费 Tool result，不复用 OpenClaw 的文本标签检测；
- **共享 `ActionProcessor`**：两轨复用自动执行、幂等、去重和脱敏等行为语义；
- **durable outbox**：先把本轮 evidence 可靠交给 Redis，再结束用户请求，PG/mem0 后台处理。

### 8.4 当前最需要补的缝

1. **没有 agent-center 统一 ToolRegistry**：静态 Tool、动态 Tool、ADK ToolNode 和 Python registry 各管一段，缺少一个执行期 policy seam。
2. **静态/动态名称冲突未统一拒绝**：Runner 直接 append 两组 Tool；动态目录内部验证不能覆盖跨来源冲突。
3. **动态参数主要用于模型 schema**：原始 arguments 会透传到 skill-service，Go/Python 两侧需要统一的严格 schema 与未知字段策略。
4. **角色与 scope 传播不足**：可信身份必须由 agent-center 签发并进入执行授权，不能只作为观测 header 或依赖默认角色。
5. **action allow-set 粒度偏粗**：snapshot 接受所有已宣传 action type，建议绑定到产生本次结果的 Tool/Skill spec。
6. **Strategy 仍全量注入**：数量增长后会重新产生 D10 的 token 问题，应演进为摘要发现、按需加载、会话固定版本。
7. **skill-service 不是 Tool 级 Sandbox**：即使部署成独立容器，动态 handler 仍可能共享 Python 进程、文件系统、网络和凭证；隔离粒度取决于部署，而不是 Skill 名称。
8. **跨轮 Tool evidence 需要独立契约**：可持久化脱敏、压缩、带 freshness 的 evidence capsule；不要直接重放缺少 tool-call ID/参数关联的原始 `role:tool` 消息。

## 9. VikingBot 与 Eino 的设计对照

两者的优势互补但部署单元不同：VikingBot/OpenClaw 更像一个用户拥有的 Agent 工作台，Eino 是许多用户共享的中央生产服务。正确方向是移植能力分层，而不是移植 per-user 进程或 workspace；Eino 必须保留显式多租户 scope、结构化协议、无状态成本模型和后端无关边界。

| 维度 | VikingBot | 当前 Eino | 建议取舍 |
|---|---|---|---|
| 运行形态 | 长运行 AgentLoop + 多 Channel | 请求级无状态 ADK runner | 保留 Eino；不要引入 per-user 常驻 loop |
| Tool 注册 | 单个 `ToolRegistry` + MCP 动态注册 | 静态 wiring + snapshot 动态 Tool + Python registry | 新增统一执行 Registry，复用 snapshot |
| Tool 结果 | 主要为字符串 | `RunEvent.ToolResult` 可保留结构 | 采用 typed result，以 Eino 为准 |
| Skill | Workspace `SKILL.md` 渐进加载 | API/Strategy 二分类；Strategy 全文注入 | 引入一等 SkillDescriptor 与按需加载 |
| Catalog 稳定性 | Registry 生命周期内稳定 | fingerprint 去重 + RunID pin | Eino 更好，继续作为 RunToolSet 版本 |
| Action | 可依赖模型后续解释 | 结构化结果 → Emitter → Processor | 保留 Eino 确定性路径 |
| Sandbox | 统一接口，多后端与 workspace mode | 无通用本地执行面 | 只为 Tier 2 Tool 增加远程 Sandbox |
| 权限 | schema 过滤为主，执行期缺二次校验 | 分散在 Tool/服务 | 两者都需统一 CapabilityPolicy |
| 并发 | 同轮 Tool 统一并发 | ADK ToolNode 可并发 | 按 ToolSpec 和 resource key 调度 |
| 观测 | ToolRegistry 统一 Langfuse/Hook | ADK callback、Emitter、业务 trace 分层 | 保留 Eino trace，Registry 补统一 tool span |
| 记忆/交付 | Session + OpenViking context | Redis History + mem0 + durable outbox | OV 替换 mem0；短期历史与异步交付保持不变 |
| Subagent | 有受限 Registry，但生命周期较轻 | 尚非当前主能力 | 先不引入；未来必须有预算与独立 workspace |

最重要的反向结论是：**Eino 已经有比 VikingBot 更好的会话快照、结构化 Action 和 backend 隔离层，不应为了“统一”而退回字符串结果、文本卡片或全局可变 Registry。** ToolRegistry 应围绕这些资产设计，而不是替代它们。

## 10. 推荐的 Eino 目标架构

目标架构应拆成 Capability、Planning、Execution、State/Delivery 四个平面，并让每次请求得到不可变的 ToolSet/SkillSet 快照。所有 Tool 调用都必须在执行期重新授权，高风险 Tool 再通过 SandboxExecutor 落地。

```text
skill-service ─► CapabilityCatalog ─► immutable RunCapabilitySnapshot
Static Go Tool ──────────┘                         │
                                                  ▼
OpenViking ◄─ MemoryProvider ◄─ outbox      Request ─► ADK / Planning
     │                    ▲                         │
     └─ bounded recall ───┘                         ▼
                                            RunToolSet / Policy
                                                  │
                                  ToolScheduler ─► Executor
                                     ├─ InProcess
                                     ├─ RemoteAPI ─► skill-service(name+version/digest)
                                     └─ Sandbox (T2 only)
                                                  │ typed result
                                                  ▼
                          EinoEmitter ─► ActionProcessor ─► SSE
                              └───────► Redis History / outbox / trace
```

### 10.1 Capability plane

`CapabilityCatalog` 统一接入静态 Go Tool、当前 skill-service descriptor 和未来 MCP，但不直接执行。注册时必须校验全局唯一的 namespace/name、schema、版本、来源、风险与 executor；冲突应 fail closed，不能后注册覆盖。API Skill 的调用必须携带 snapshot 固定的 `name + version/digest`，不能让 v1 指令在热加载后调用同名 v2 handler。

建议把当前 `SkillDescriptor` 扩展为：

```text
name, version, summary, instructions_ref, class,
required_tools, allowed_tools, requirements,
risk, sandbox_profile, output_contract, action_types
```

普通轮次只披露 Skill summary。模型选择 Skill 后，通过受控的内部 `load_skill(name, version)` Tool 读取已固定 snapshot 中的指令；大陆 Eino 不需要为了模仿 VikingBot 而开放通用 `read_file`。`load_skill` 是控制面读取 Tool，不产生业务副作用，也不能修改本轮权限。

### 10.2 Planning plane

继续使用 ADK/ReAct 和 `internalRunner`。Runner 不再直接 append 静态/动态 slices，而是拿到一个不可变 `RunCapabilitySnapshot`：其中包含 Tool definitions、Skill 摘要、版本和 action contract；其 fingerprint 随 durable session/request 传播，不能只存在单副本内存。Skill 加载后只能使用 `allowed_tools ∩ RunToolSet`，永远不能扩权。

### 10.3 Execution plane

统一 ToolRegistry 的执行顺序应固定为：

```text
resolve snapshot tool
→ execute-time authorization
→ strict argument validation
→ approval/idempotency check
→ concurrency scheduling
→ choose executor
→ timeout + bounded output
→ typed result + audit/evidence
```

Executor 按风险分层：

| Tier | Tool | Executor | 额外控制 |
|---|---|---|---|
| T0 | 纯函数、只读 Go 查询 | InProcess | timeout、限流、输出上限 |
| T1 | 远程 API、业务写入 | RemoteAPI | actor/scope、幂等、审批、审计 |
| T2 | 文件、Shell、代码、浏览器、动态脚本 | Sandbox | per-run/session 隔离、egress 与资源策略 |

T1 不因“有副作用”就自动需要 OS Sandbox；它需要的是正确业务授权。T2 即使只读也可能读取宿主密钥，因此必须隔离。

### 10.4 Memory、State 与 Delivery plane

职责必须单一：Redis History 保存最近逐字消息并服务 prefix cache；OpenViking 保存跨 session 长期记忆；raw evidence/outbox 提供可重放交付；Eino 只消费 provider-neutral context cards。Skill 仍由 skill-service 管，不因采用 OV memory 就同时建立第二套 Skill 控制面。

`pkg/memory` 是合适的防腐层起点，但应先扩展 server-derived tenant/account scope，并用 `AddEvent{event_id, scope, messages}` 取代无幂等键的写接口。第一阶段 OV adapter 可把检索结果压平为现有 `MemoryCard{text, score}`，不改 ADK；只有评测证明 summary/URI 渐进读取有价值时，才增加 backend-neutral opaque ref 与 `read_memory_ref`，不要让 Eino 依赖 `viking://` 细节。

写路径继续保持 `ArchiveTurn → Redis Stream → background Memory.Add`。迁移期若双写 mem0/OV，必须把完成标记拆成 per-target 状态并为 OV 传递稳定 event id；否则“远端成功、写标记前崩溃”会重复提交。每个 Tool 调用可另形成脱敏 `ToolEvidence` capsule，但不能直接重放脆弱的原始 tool transcript。

## 11. 一个简洁的复杂工作流示例

“分析用户上传的 CSV，生成结论与报告文件”同时需要 Skill、Tool 和 Sandbox：Skill 定义步骤与验收标准，Tool 提供文件和命令原语，per-session Sandbox 隔离不可信数据与生成代码。

假设用户说：“分析这个销售 CSV，找出异常并生成 Markdown 报告。”

```text
1. 模型从摘要发现 csv-analysis Skill，调用 load_skill。
2. Skill 要求：校验数据 → 分析 → 质量检查 → 生成并发布报告。
3. Registry 确认本轮允许 artifact_read、run_analysis、artifact_write、publish_artifact，
   且该 Skill 要求 sandbox_profile=python-data-readonly-netoff。
4. SandboxExecutor 创建 per-run 环境，只读挂载输入 CSV，默认断网。
5. Tool 顺序执行；质量检查失败则结果回灌模型修正，不发布半成品。
6. publish_artifact 只提升最终 report.md，返回 artifact_ref 与摘要。
7. Emitter 回复结论和下载引用；evidence 通过 outbox 异步保存。
```

这里 Skill 决定步骤、失败处理和验收；Tool 提供可审计原语；Sandbox 限制文件、进程、网络和资源。若只是调用一个服务端 `analyze_csv(object_ref)` API，则它可以是 T1 Tool，不必再启动通用代码 Sandbox。

## 12. 分阶段落地建议与验收标准

建议先补执行治理，再做 Skill 一等化，最后只为高风险能力引入 Sandbox；这样不会破坏 Eino 当前按并发扩缩的成本优势，也不会把 Python skill-service 或每用户常驻环境重新变成默认依赖。

### P0：先补 Tool 执行治理

- 盘点静态 Go Tool、动态 API Skill 和 Strategy Skill，给出唯一名称、origin、risk、side effect、scope、timeout、并发和输出契约；
- 新建统一 Registry/Executor seam，让静态与动态 Tool 都经同一入口；
- 跨来源重名直接拒绝，执行时再次检查 snapshot 与 scope，参数拒绝未知字段；
- 引入 typed `ToolResult`，把 `action_type` 绑定到具体 ToolSpec；
- 保持 `SnapshotProvider` 的 fingerprint、RunID pin 和 last-known-good，不重写 ADK loop。

**P0 验收**：隐藏/未授权 Tool 即使被伪造 tool call 也不能执行；同名 Tool 不能启动；写 Tool 不会错误并发；每次调用有统一 trace、错误码和结果上限。

### P1：把 Skill 变成一等能力

- 将 Strategy 从“永久全文 system prompt”迁到 summary + `load_skill`；
- descriptor 增加 version、required/allowed tools、requirements、risk、output contract；
- session snapshot 同时固定 Skill 和 Tool 版本，热更新只影响新 session；
- 记录 Skill 被发现、加载、执行成功和 fallback 的指标，用真实 token/成功率决定哪些 Skill 可 always-load。

**P1 验收**：未选择 Skill 不注入全文；Skill 不能调用 allowlist 外 Tool；指令与 handler 的 version/digest 一致。skill-service 失联时，冷启动无快照、目录可用但执行不可用、完全健康三种状态分别可观测，并由健康门/熔断决定是否隐藏 API Skill。

### P2：用 OV 替换 mem0，不改变轻内核

- 版本化扩展 `pkg/memory` 的 tenant scope 与 event-id 写契约，再实现 OV adapter；映射由 adapter 独占；
- 写入仍走 durable outbox，OV 超时或不可用不能推迟 `[DONE]`；请求内 Search 设小预算并保持无记忆降级；
- 先 shadow-write + sampled shadow-read 比较，模型只注入一个主来源，避免重复记忆污染 Prompt；
- 双写期间分别记录 mem0/OV 成功状态和积压；OV 主读稳定后再停 mem0 写入并排空已接受事件。

**P2 验收**：同用户可跨 session 召回、不同用户严格隔离；切换不改变 Eino/ADK 接口；OV 故障不阻塞交付；重试不会产生不可控重复记忆。

### P3：为 T2 能力引入远程 Sandbox

- 先定义 `ExecutionEnvironment` contract，再选 OpenSandbox-like 容器服务；
- 默认 per-run scratch，确需跨轮文件时才用 per-session；产物进入对象存储；
- 不注册通用 file/exec/code Tool，直到隔离 contract tests 通过；
- backend 启动失败 fail closed，生产禁止回退 `TrustedDirect`。

**P3 验收**：跨租户读、宿主 secret、localhost/私网/metadata、超时残留进程、CPU/内存/磁盘/输出逃逸均有可失败测试；binary artifact 跨边界可用。

### P4：再考虑 Subagent、HITL 与长期 Tool evidence

Subagent 只有在单 Agent 的上下文或并行规划成为实际瓶颈后再引入，并且必须有独立预算、deadline、取消、最大并发和 workspace。高风险写操作先走 HITL/审批；跨轮只保存脱敏 evidence capsule，不保存无法可靠关联 call ID 的裸 `role:tool`。

当前不建议做三件事：给所有大陆用户创建常驻 workspace；把所有 Go Tool 搬到 Python skill-service；为了统一两轨而放弃 Eino 的结构化 Action。它们都会增加成本或降低确定性，却不解决真正的能力治理问题。

## 参考源码

下面的链接是本文结论的源码入口；行号会随代码演进漂移，因此以类名、函数名和字段名作为长期定位锚点。

### VikingBot

- [VikingBot 概念总览](../../en/concepts/15-vikingbot.md)
- [AgentLoop](../../../bot/vikingbot/agent/loop.py)、[ContextBuilder](../../../bot/vikingbot/agent/context.py)、[SkillsLoader](../../../bot/vikingbot/agent/skills.py)
- [Tool 基类与 ToolContext](../../../bot/vikingbot/agent/tools/base.py)、[ToolRegistry](../../../bot/vikingbot/agent/tools/registry.py)、[默认 Tool 工厂](../../../bot/vikingbot/agent/tools/factory.py)、[MCP Tool 适配](../../../bot/vikingbot/agent/tools/mcp.py)
- [SandboxManager](../../../bot/vikingbot/sandbox/manager.py)、[SandboxBackend](../../../bot/vikingbot/sandbox/base.py)、[Direct](../../../bot/vikingbot/sandbox/backends/direct.py)、[SRT](../../../bot/vikingbot/sandbox/backends/srt.py)、[OpenSandbox](../../../bot/vikingbot/sandbox/backends/opensandbox.py)、[AIO Sandbox](../../../bot/vikingbot/sandbox/backends/aiosandbox.py)
- [Sandbox 与 Langfuse 配置](../../../bot/vikingbot/config/schema.py)、[HookManager](../../../bot/vikingbot/hooks/manager.py)

### xkong-agent-center / Eino

- [通用 Agent 双轨架构](../../../../xkong-agent-center/docs/design/通用agent双轨架构/main.md)
- [Runner 中立事件契约](../../../../xkong-agent-center/internal/backend/eino/runner.go)、[ADK adapter 与静态/动态 Tool 合并](../../../../xkong-agent-center/internal/backend/eino/adk_runner.go)
- [会话能力快照](../../../../xkong-agent-center/internal/backend/eino/snapshot.go)、[SnapshotProvider](../../../../xkong-agent-center/internal/backend/eino/snapshot_provider.go)、[skill-service Tool adapter](../../../../xkong-agent-center/internal/backend/eino/tool_skill_service.go)
- [EinoEmitter](../../../../xkong-agent-center/internal/backend/eino/emitter.go)、[Eino 启动 wiring](../../../../xkong-agent-center/internal/server/eino_wiring.go)
- [provider-neutral Memory contract](../../../../xkong-agent-center/pkg/memory/memory.go)、[Durable evidence outbox](../../../../xkong-agent-center/internal/memory/evidenceoutbox/consumer.go)
- [VikingBot 的 OV session append/commit 适配](../../../bot/vikingbot/openviking_mount/ov_server.py)
