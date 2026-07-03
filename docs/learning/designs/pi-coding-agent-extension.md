# Pi Coding Agent OpenViking Extension — Design Summary

> 一句话结论：这个扩展把 Pi coding agent 接到 OpenViking 长期记忆库，用「当前 prompt 同步召回 + 每轮异步归档 + commit 后抽取长期记忆」替代 Hermes 式 stale prefetch，目标是在不污染记忆库的前提下，让 Pi 每轮都拿到相关历史上下文。

## TL;DR

Pi 扩展的核心设计是两条并行路径：**读路径**在模型调用前用当前用户 prompt 搜索 OV，并把结果注入 `<relevant-memories>`；**写路径**在每轮结束后清洗对话、批量写入 OV session，并在 token 阈值、压缩前、会话结束时 commit。设计主要继承 Claude Code 插件的成熟策略，保留 OpenClaw 的同步召回优点，并明确避开 Hermes 的「上一轮预取、下一轮才用」问题。

关键收益是：第一轮就能召回相关记忆，话题切换不会拿错上下文，compaction 前内容不会丢，注入块不会被再次写回 OV 形成污染循环。

## Architecture

扩展是一个零 npm 依赖的 TypeScript 目录，通过 Pi 的 extension API 注册事件和工具，所有持久化与检索都通过 OV REST API 完成。模块边界清晰：`client.ts` 包 HTTP，`recall.ts` 做自动召回，`sync.ts` 做会话写入和 commit，`index-builder.ts` 做知识目录，`tools.ts` 暴露模型可调用工具，`index.ts` 负责事件编排。

```text
Pi event loop
  session_start / before_agent_start / context / turn_end / compaction / shutdown
        |
        v
Pi OpenViking extension
  client.ts  recall.ts  sync.ts  index-builder.ts  tools.ts
        |
        v
OpenViking server
  sessions, search, content read, fs browse, resource ingest
```

设计中的模块职责：

| File | Role |
|---|---|
| `client.ts` | OV HTTP client, including health, session, search, content, fs, resource APIs |
| `recall.ts` | Search current prompt, filter, dedupe, rerank, format `<relevant-memories>` |
| `sync.ts` | Strip injected blocks, filter low-signal turns, queue writes, commit sessions |
| `index-builder.ts` | Build a compact `viking://` knowledge index for the system prompt |
| `tools.ts` | Register `viking_search`, `viking_read`, `viking_browse`, `viking_remember`, etc. |
| `index.ts` | Wire Pi events to client, recall, sync, index, and tools |

## Context Model

The design deliberately splits context into a **map** and a **flashlight**. The map is the memory index: a compact system-prompt summary of what OV knows. The flashlight is recall: a per-prompt search result block for the current task.

The memory index is built from `viking://` directory listings and memory abstracts. It helps the model know that useful knowledge exists before it decides whether to call `viking_search` or `viking_read`. It is not full memory content, because full content would waste the system prompt on mostly irrelevant data.

Recall runs against three scopes:

| Scope | Meaning |
|---|---|
| `viking://user/<space>/memories` | User personal memories, preferences, entities, prior decisions |
| `viking://agent/<space>/memories` | Agent operational memories and lessons |
| `viking://agent/<space>/skills` | Stored procedures and reusable skills |

Resources are intentionally excluded from automatic recall to avoid cross-namespace leakage. They remain searchable on demand through `viking_search` with an explicit scope.

## Recall Pipeline

Recall is synchronous and current-turn: the query is the user's current prompt, not the previous turn. This is the main design correction over Hermes, where stale prefetch can inject wrong-topic memory.

The pipeline is:

1. Skip very short prompts under `recallMinQueryLength`.
2. Resolve multi-tenant `viking://user/memories` and `viking://agent/memories` into fully qualified spaces.
3. Run three parallel `client.find()` calls across user memories, agent memories, and agent skills.
4. Build a local query profile with token extraction and regex intent checks. This does not require an extra LLM call.
5. Filter results below `recallScoreThreshold`.
6. Deduplicate events/cases by URI, and other results by lowercased abstract text with URI fallback.
7. Rerank with category boosts and lexical overlap.
8. Resolve content, cap each item, and format a token-budgeted `<relevant-memories>` block.

Deduplication is string-based, not vector-based. For non-event results, two hits dedupe only when their abstract text matches after `toLowerCase().trim()`. Near-synonyms with different text remain separate unless they share a URI fallback.

## Capture And Commit

The write path preserves useful conversation signal without feeding injected context back into the memory system. Each `turn_end` extracts user text, assistant text, and tool-use inputs, then queues clean messages into the OV session.

Before writing, `sync.ts` strips synthetic blocks:

| Block | Why strip it |
|---|---|
| `<relevant-memories>` | Prevent recalled memory from being re-indexed as new conversation |
| `<openviking-context>` | Prevent profile/index context from becoming user-authored memory |
| `<system-reminder>` | Remove framework reminders |
| `[Subagent Context]` | Avoid parent/subagent context leakage |
| null bytes | Clean encoding artifacts |

The capture filter skips empty turns, tiny acknowledgments, slash commands, punctuation-only text, and pure question-only turns. In `semantic` mode it captures substantive turns by default; in `keyword` mode it requires memory-like trigger phrases.

Commits happen through three triggers:

| Trigger | Behavior |
|---|---|
| Token threshold | Flush queued turns, then `commit(wait=false)` once pending text crosses `commitTokenThreshold` |
| Pre-compact | Flush and `commit(wait=true)` before Pi rewrites transcript history |
| Shutdown | Flush and `commit(wait=true)` as the final safety net |

## Safety Choices

The design treats memory quality as the main failure boundary. A bad recall system is worse than no recall if it injects stale, duplicated, or self-referential context into the model.

Important safety choices:

| Choice | Reason |
|---|---|
| Health-check once at session start | If OV is down, degrade to no-op instead of spamming failures |
| Bypass patterns by cwd | Scratch directories should not pollute durable memory |
| URI space resolution | Multi-user deployments cannot assume `default` namespaces |
| CJK-aware token estimates | `chars / 4` undercounts Chinese, Japanese, Korean, and fullwidth text |
| Tool input preservation, result dropping | Agent intent is useful; raw tool output is usually noisy and huge |
| Write queue retry | Failed writes stay queued instead of being silently discarded |

## Tool Surface

The extension exposes seven model tools. The important distinction is that automatic recall is narrow and budgeted, while tools let the model fetch depth on demand.

| Tool | Purpose |
|---|---|
| `viking_search` | Semantic search over OV knowledge |
| `viking_read` | Read a `viking://` URI at abstract, overview, or full detail |
| `viking_browse` | List or stat the OV filesystem tree |
| `viking_remember` | Store a fact as session content for later extraction |
| `viking_forget` | Delete or search-delete stale memory |
| `viking_add_resource` | Ingest a URL or file as a resource |
| `viking_archive_expand` | Expand archived session content when summaries are too coarse |

## Design Trade-offs

This design spends a small amount of latency on current-turn retrieval to avoid stale context. The target cost is a short synchronous search before model execution; the benefit is that first turns and topic switches get relevant memory immediately.

Other trade-offs:

| Trade-off | Chosen side |
|---|---|
| Full memory in prompt vs index + recall | Use compact index plus targeted recall to preserve token budget |
| One HTTP write per turn vs queued writes | Queue writes to reduce overhead and retry failed writes |
| Tool results in memory vs tool inputs only | Drop results by default to avoid indexing large noisy output |
| Exact dedupe vs semantic dedupe | Use exact URI/abstract dedupe for cheap deterministic behavior |
| Session-end-only commit vs threshold/pre-compact/shutdown | Commit earlier to reduce crash and compaction loss windows |

## Implementation Notes

The current code mostly follows the design, with one notable drift: `DESIGN.md` describes recall search as happening in `before_agent_start` and `context` as reinjection; `README.md` says the `context` event searches. The implementation in `index.ts` matches the design: `before_agent_start` calls `recall.searchAndCache(event.prompt)`, and `context` calls `recall.injectRecall(event.messages)`.

The current implementation also has a few simplified edges compared with the full spec: `index-builder.ts` builds user-memory and resource summaries but not the full archive listing shown in the design example, and `sync.ts` uses a fixed `24000` length bound inside `shouldCapture()` rather than reading `captureMaxLength` in that helper. Those are implementation gaps to verify before treating the spec as fully shipped behavior.

## Source Map

Use these files as the primary references when changing the extension. The design doc explains intent; the TypeScript files define current behavior.

| Source | What to read it for |
|---|---|
| `examples/pi-coding-agent-extension/DESIGN.md` | Full design rationale, comparison tables, event flows |
| `examples/pi-coding-agent-extension/README.md` | User-facing install/config summary |
| `examples/pi-coding-agent-extension/index.ts` | Pi event wiring and prompt injection |
| `examples/pi-coding-agent-extension/recall.ts` | Search, profiling, dedupe, rerank, formatting |
| `examples/pi-coding-agent-extension/sync.ts` | Capture filtering, stripping, write queue, commit triggers |
| `examples/pi-coding-agent-extension/index-builder.ts` | Memory index generation |
| `examples/pi-coding-agent-extension/tools.ts` | Registered tool schemas and handlers |
