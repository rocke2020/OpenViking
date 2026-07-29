# Session Memory Extraction Flow

> **TL;DR:** Session commit archives the complete source first, then Phase 2
> processes an immutable prompt copy through deterministic token-bounded windows.
> Authored text is covered losslessly, bulky tool output keeps a head/tail
> preview, and every model request has a final estimated-input guard.

This document records the current implementation. It is the main-flow reference
for changes to session memory extraction, not a proposal for a future pipeline.

## Policy

`memory_policy` decides which destinations and memory types Phase 2 may update;
it does not change the bounded-input safety rules.

```json
{
  "self": { "enabled": true },
  "peer": { "enabled": false },
  "working_memory": { "enabled": false },
  "memory_types": ["profile", "preferences"]
}
```

When `memory_types` is omitted or `null`, all enabled schemas from
`MemoryTypeRegistry` are allowed, including custom prompt/schema types. When it
is set, extraction is limited to those names for both self and peer writes.
When `working_memory.enabled` is `false`, commit still archives messages and
runs configured memory extraction, but skips the archive summary.

## Memory Type Groups

The v3 runtime processes user and agent-derived memories from the same bounded
window, while their existing storage and routing contracts remain separate.

| Group | Types | Target |
| --- | --- | --- |
| User memory extraction | Enabled registry schemas with `stage: user` | Self and peer |
| Agent-derived extraction | `cases`, `trajectories`, `experiences` | Self only |
| Session skills | `SESSION_SKILL_MEMORY_TYPE` output | Self only |

Memory schemas default to `stage: user` and `peer_enabled: true`. Set
`stage: agent` for agent-derived schemas. Set `peer_enabled: false` for
user-stage schemas that should ignore `peer_id` and `ranges` peer targets and
remain under the current user space, for example `cases`.

## Commit Main Flow

Phase 1 preserves the authoritative archive; Phase 2 derives bounded prompt
copies and never rewrites the archived messages or externalized tool objects.

Implemented in `openviking/session/session.py`:

1. Load the session-level policy from session metadata.
2. Archive the current message batch and clear the committed live messages.
3. Build deterministic Phase 2 fragments and turn-aware windows from the
   archived batch.
4. Split an individually oversized `TextPart` or `ContextPart` into stable
   plan-version, offset, and digest fragments. Across all windows, authored
   text remains in source order without omission.
5. Keep tool output as a copied head/tail preview plus its raw-storage
   reference. Do not hydrate the raw result back into model input. Structured
   `tool_input` remains unchanged; planning fails closed if that metadata alone
   cannot fit a window.
6. If usage reporting is enabled, hydrate a separate copy for the rule-based
   reporter only.
7. Run Working Memory and v3 memory extraction over the bounded windows.
8. After each successful memory window, append its audit diff and persist the
   completed fragment IDs. Retry skips recorded windows and retains their
   existing audit entries.
9. Update archive metadata and write `.done` only after every required Phase 2
   step succeeds.

## Working Memory Reduction

Working Memory folds windows sequentially, so no summary-generation request
reconstructs the full archive.

For window `N`, the previous seven-section overview is the reducer state and
window `N` is the new input. Multi-window mode requires every model result to
retain all seven canonical sections; malformed or unavailable results fail the
Phase 2 task instead of publishing a generic partial overview.

Checkpoint source IDs are remapped from archive-message IDs to the derived
fragment IDs visible in each window. If one checkpoint spans multiple windows,
its window-local notes are merged pairwise through separately guarded model
requests before the existing retained-context budget is applied.

## V3 Memory Reduction

The runtime factory always creates `SessionCompressorV3`; it processes remaining
windows sequentially and uses persisted memory as the reducer between windows.

Each window covers user memories plus the enabled case, trajectory, experience,
and skill paths. Every downstream request receives the same explicit
`request_max_tokens` value, including process-global streaming updater and
trainer work. Shared workers attach the strictest non-null budget to each
batched chunk and store an unscoped base context, so a Phase 2 request cannot
leak its cap into later non-session work.

The retained `SessionCompressorV2` injection interface does not gain v3's
windowed reduction. It receives the final request cap and fails before a
provider call if its full-archive request is oversized.

## Provider-Bound Request Guard

Every Phase 2 provider call is estimated immediately before invocation; known
tool-result envelopes may be compacted further, but authored content and system
contracts are never silently truncated.

`ExtractLoop` includes the messages and actual tool schemas for that iteration
in its estimate. It recognizes the JSON envelope produced by
`add_tool_call_pair_to_messages()` and may reduce only its `result` field to a
head/tail preview while preserving structured result shape and metadata.
`ExtractLoop.run()` invokes provider message preparation before building
schemas or prompts, so V3 user, trajectory, and experience paths all route
images through the guarded description request. Direct Working Memory and
checkpoint-reduction calls use the same scoped guard. Residual overflow raises
`ResourceExhaustedError` without calling the provider.

The relevant configuration is:

| Field | Meaning | Default |
| --- | --- | --- |
| `memory.phase2_window_max_tokens` | Maximum estimated archive-derived tokens in one Phase 2 window | `12000` |
| `memory.extraction_request_max_tokens` | Maximum estimated serialized tokens in one Phase 2 model request | `32768` |

The request cap must be greater than or equal to the window cap.

## Long-Term Routing

Final write targets are resolved per operation, and each window may address
only peers evidenced inside that same window.

Implemented in
`openviking/session/memory/memory_isolation_handler.py`,
`MemoryIsolationHandler.calculate_memory_uris` resolves operations as follows:

| Operation fields | Result |
| --- | --- |
| No `peer_id`, no `ranges` | Write self if self memory is enabled |
| Safe `peer_id` observed in this window | Write that peer |
| Unsafe or window-unobserved `peer_id` | Skip |
| `ranges` present | Resolve against the window-local `ExtractContext` |
| Schema has `peer_enabled: false` | Ignore peer targets and write self if enabled |
| Only disabled targets found | Skip |

The router does not rewrite message roles. Tool evidence remains attached to
the message where it was recorded.

## Storage Targets

Self and peer operations keep the existing namespace layout; bounded input does
not alter storage paths.

For current user space `viking://user/<user_id>`:

| Target | Storage space |
| --- | --- |
| Self | `viking://user/<user_id>/...` |
| Peer | `viking://user/<user_id>/peers/<peer_id>/...` |

Peer-only extraction does not initialize self default files. Default self files
are initialized only when self memory is enabled.

## Practical Invariants

The safety boundary is source-preserving and provider-bounded: all derived
windows are replayable from the archive, but no individual model request may
exceed the configured input cap.

- Archived messages and externalized tool objects remain unchanged.
- All authored text appears across Phase 2 windows exactly once and in order.
- Tool evidence exposed to agent-memory paths retains both its head and tail.
- Working Memory, user memory, and agent-derived v3 paths consume bounded
  windows and never concatenate them back into one archive-sized prompt.
- Completed-window memory diffs survive retry.
- Process-global worker contexts do not retain request-local budgets.
- A residual oversized request fails before provider invocation.
