# 上下文管理 — Context Management Summary

> 三套 agent memory 插件（Claude Code / Pi / Codex）如何与 OV server 协作，把会话 token 量压在可控范围内并触发归档与记忆抽取。

## TL;DR

OV 的上下文管理分两层：**客户端驱动 commit**（默认 20000 pending tokens 阈值）和**服务端 Session 自动归档**（`auto_commit_threshold=8000`，但 `addMessage` 不消费它，所以实际由客户端 polling 触发）。当累积 `pending_tokens` 越过阈值时，插件调用 `commitSession`，OV server 异步生成 archive 并抽取长期记忆，把上下文从「越聊越长」压缩成「分段归档 + 按需召回」。

## 两层阈值

### 客户端阈值（默认 20000）

| 位置 | 配置项 | 默认 | 说明 |
|---|---|---|---|
| `examples/claude-code-memory-plugin/scripts/config.mjs:254` | `OPENVIKING_COMMIT_TOKEN_THRESHOLD` | `20000` | CC 插件，min 1000 |
| `examples/pi-coding-agent-extension/config.ts:51` | `commitTokenThreshold` | `20000` | Pi 扩展 |
| `examples/pi-coding-agent-extension/config.json:18` | `commitTokenThreshold` | `20000` | 发布配置 |

**触发逻辑**（CC 与 Pi 一致）：每轮结束后读取 server 返回的 `pending_tokens`，若 `>= threshold` 则调用 `commitSession(wait=false)`。归档与记忆抽取在 OV server 侧异步进行，不阻塞 agent。

参考文件：
- CC: `examples/claude-code-memory-plugin/scripts/auto-capture.mjs:630-642`
- Pi: `examples/pi-coding-agent-extension/sync.ts:227-231`

### 服务端阈值（8000，但未被消费）

`openviking/session/session.py:362` 的 `Session.__init__` 接受 `auto_commit_threshold: int = 8000`，存为 `self._auto_commit_threshold`。但 `auto-capture.mjs:624` 注释明确说明：

> OV's `Session._auto_commit_threshold` is not consumed by addMessage, so we poll pending_tokens ourselves and commit when the threshold is crossed.

所以 **8000 只是构造默认值，真正决定归档节奏的是客户端的 20000 阈值**。Codex 插件没有客户端阈值，依赖服务端路径（其 `120000` 命中是 `OPENVIKING_RECALL_TIMEOUT_MS` / `OPENVIKING_CODEX_ACTIVE_WINDOW_MS`，与 commit 阈值无关）。

## 三种 commit 触发方式

以 Pi 扩展 `DESIGN.md:432-434` 为准，三套插件共用同一思路：

1. **Threshold commit** — `pendingTokens >= commitTokenThreshold` 时 `commit(wait=false)`。基于 token 而非 turn，因为「1 行 ack」和「10 次工具调用」的轮次内容量差几个数量级。崩溃时，turn 1-70 的记忆已落盘。
2. **Pre-compact commit** — agent 触发 compaction 前先 `commit(wait=true)`，否则被压缩掉的内容对 OV 永久丢失。CC 的 `PreCompact` hook、Pi 的 `session_before_compact` 事件做同样的事。
3. **Session-end commit** — 会话结束时兜底 flush。

## 为什么用 token 而非 turn

`examples/pi-coding-agent-extension/DESIGN.md:434`：

> Token-based is more accurate than turn-based — a 1-line ack and a 10-tool-call turn are very different content volumes.

按轮次触发会导致：短对话频繁归档（浪费），长工具链轮次迟迟不归档（崩溃丢数据风险）。token 累积直接对应 OV 实际要处理的内容量，是更准的代理指标。

「1 行 ack」= 一行确认回复（如「好的」「Done.」），token 量极小；与之对比的是 10 次工具调用 + 大段输出的轮次。两者按 turn 计都被算成「1」，对 OV 实际要 digest 的内容量是失真的。

## 为什么是 20000 而非更小值（如 4092）

**结论：20000 是从 openclaw 移植来的历史默认值，未经调优，不是被实测出来的最优解。**

`examples/claude-code-memory-plugin/scripts/config.mjs:252-253`：
```js
// P0-2: client-driven commit threshold (ported from openclaw afterTurn).
// Default 20000 aligns with openclaw; lower values produce archives faster.
```

`DESIGN.md` 只论证了「token vs turn」，没有论证「20000 vs 其他数值」。可推的工程理由：

**用更小值（如 4092）的代价：**
- 单次 archive 内容太少，memory 抽取喂的上下文不够 → 抽出的记忆碎片化、质量差。
- commit 次数翻几倍 → server 端 archive 生成 + LLM 记忆抽取调用量翻几倍（成本 / 速率限制压力）。
- statusline 锯齿（sawtooth）更密，抖动更频繁。

**用 20000 的隐含取舍：**
- 一次 archive 沉淀 ~20k tokens 连续上下文，足够 LLM 抽出有意义的语义记忆（事件、决策、实体关系），而非零碎片段。
- 仍小于典型 agent 上下文窗口的 5-10%，崩溃丢数据窗口可控。
- 4092 约对应 3-5KB 文本，可能连一次中等工具调用 + 输出都没攒够就触发 commit，archive 价值低。

**未验证（应当调优而非照搬的点）：**
- 20000 对长会话（几百轮）是否最优？未知。
- CC 与 Pi 工具调用密度不同，共用同一默认值是否合适？未知。
- 10000 / 40000 等中段的 recall 质量 vs 成本曲线？仓库无数据。

如需调优，应跑对照（如 4092 / 10000 / 20000 / 40000）测 recall 质量与 commit 成本的权衡，目前仓库无此基准数据。

## 召回侧（recall）

记忆抽取在 commit 后异步进行；召回则在 `UserPromptSubmit` / `session_start` 时由插件自动触发，把相关 archive 概要注入上下文。这部分由 `resumeContextBudget`（默认 32000）等预算项控制，与 commit 阈值正交。

## 关键文件索引

- 服务端 Session: `openviking/session/session.py:362`
- CC 插件 config: `examples/claude-code-memory-plugin/scripts/config.mjs:254`
- CC 插件触发: `examples/claude-code-memory-plugin/scripts/auto-capture.mjs:636`
- Pi 扩展 config: `examples/pi-coding-agent-extension/config.ts:51`
- Pi 扩展触发: `examples/pi-coding-agent-extension/sync.ts:227`
- Pi 设计文档: `examples/pi-coding-agent-extension/DESIGN.md:432-434`
- Codex 设计文档: `examples/codex-memory-plugin/DESIGN.md`

## Related

- [openviking-server.md](./openviking-server.md) — 本地 OV server 启动与编辑-重启循环
