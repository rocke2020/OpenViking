# OpenClaw Session-End Final Commit

> **TL;DR:** [OpenViking PR #3482](https://github.com/volcengine/OpenViking/pull/3482)
> closes a lifecycle gap in the OpenClaw plugin. Active sessions still use token-threshold
> commits that retain recent context, but `session_end` and `before_reset` now perform a final
> `commit(wait=false, keepRecentCount=0)`. OpenViking archives all remaining live messages before
> returning from the commit request, while memory extraction continues asynchronously.

This note explains the workflow and design of commit
[`6a14bc3e`](https://github.com/volcengine/OpenViking/commit/6a14bc3edbad0c907125571ae8803d49feb22b12).
The PR is not part of the `dev` branch at the time this note was written, so the links below use
the immutable PR commit rather than local line numbers.

## 1. The problem

**Periodic token-threshold commits were not enough to guarantee that the end of a session was
archived. A short session could finish below the threshold and leave its final messages in the
root `messages.jsonl` indefinitely.**

During an active conversation, the OpenClaw plugin appends completed turns to the OpenViking
session and reads `pending_tokens`. It commits only after the configured fraction of the model's
token budget has accumulated:

```text
afterTurn
    |
    +-- append messages to root messages.jsonl
    +-- read pending_tokens
    |
    +-- pending_tokens below threshold
    |      `-- leave messages live
    |
    `-- threshold reached
           `-- commit(wait=false, keepRecentCount=configured value)
```

The periodic path normally retains recent messages so the active session can continue with a
live context window. Before PR #3482, `session_end` only remembered routing identity and did not
commit:

```ts
deps.api.on("session_end", async (_event, ctx) => {
  deps.rememberSessionAgentId(ctx ?? {});
});
```

`before_reset` did commit, but it was not a complete substitute. OpenClaw can end sessions through
new-session creation, reset, idle expiry, deletion, gateway shutdown, restart, or other lifecycle
paths. A terminal event therefore needed its own final drain.

## 2. The two commit workflows

**The solution separates periodic commits from terminal commits because they serve different
purposes. Periodic commits preserve a live tail; terminal commits archive the tail completely.**

| Workflow | Trigger | `wait` | `keepRecentCount` | Result |
|---|---|---:|---:|---|
| Periodic | `pending_tokens` reaches the configured threshold | `false` | Configured live-window count | Archive older messages and retain recent context |
| Terminal | `session_end` or `before_reset` | `false` | `0` | Archive every remaining live message |
| Explicit/default helper | Existing callers that omit options | `true` | `0` | Preserve the pre-PR synchronous helper behavior |

The combined lifecycle is:

```text
Active session
    |
    +-- afterTurn
    |      +-- below threshold -> keep accumulating
    |      `-- threshold reached -> periodic commit, retain live tail
    |
    `-- session_end / before_reset
           `-- final commit, retain nothing
```

This design does not lower the token threshold. Lowering it would reduce the number of stranded
short sessions but would not create a terminal guarantee. The fix adds the missing terminal
boundary instead.

## 3. Terminal hook workflow

**Both terminal hooks call one `finalizeSession` function, so bypass rules, identity resolution,
deduplication, logging, and failure cleanup have a single implementation.**

The hook follows these steps:

1. Reject sessions matched by configured bypass patterns.
2. Stop if the event has no session ID or the context engine is unavailable.
3. Resolve the OpenClaw `sessionId` and `sessionKey` into the canonical OpenViking session ID.
4. Reuse an existing in-flight terminal commit for that OpenViking session.
5. Otherwise call:

   ```ts
   contextEngine.commitOVSession(
     { sessionId, sessionKey },
     { wait: false, keepRecentCount: 0 },
   );
   ```

6. Log success or convert a thrown/rejected failure into `false`.
7. Remove the completed promise from the in-flight map.

`session_end` first remembers the agent/session identity, then finalizes the session.
`before_reset` directly uses the same finalizer.

Primary implementation:
[openviking-lifecycle-hooks.ts](https://github.com/volcengine/OpenViking/blob/6a14bc3edbad0c907125571ae8803d49feb22b12/examples/openclaw-plugin/plugin/openviking-lifecycle-hooks.ts).

## 4. Why `keepRecentCount=0`

**A terminal commit must retain zero messages because a retained live window has no future turn
that can make those messages archive-eligible.**

For a periodic commit with a retained count of ten:

```text
live messages: M1 ... M30
periodic commit:
    archive: M1 ... M20
    root:    M21 ... M30
```

That overlap is useful while the conversation continues. New messages gradually push the retained
messages outside the live window, making them eligible for a later periodic archive.

At session end there may be no later message and no later threshold check:

```text
terminal commit:
    archive: every remaining root message
    root:    empty
```

OpenViking implements this split under a session path lock. A positive retained count divides the
message list into an archive prefix and retained suffix; zero copies the full list into the archive
and leaves an empty retained list.

Server implementation:
[session.py](https://github.com/volcengine/OpenViking/blob/6a14bc3edbad0c907125571ae8803d49feb22b12/openviking/session/session.py#L1156-L1185).

## 5. Why `wait=false` still protects the archive

**`wait=false` skips client-side polling for memory extraction, not the archive write. The commit
HTTP request returns only after OpenViking has completed Phase 1 and queued Phase 2.**

OpenViking commit has two phases:

```text
POST /sessions/<id>/commit
    |
    v
Phase 1: inline archive transaction
    +-- acquire the session path lock
    +-- write history/archive_NNN/messages.jsonl
    +-- rewrite root messages.jsonl with the retained tail
    +-- reset pending_tokens and update metadata
    +-- enqueue a persistent SessionCommitMsg
    `-- return status=accepted and task_id
                         |
                         v
Phase 2: asynchronous queue worker
    +-- generate archive L0/L1
    +-- extract long-term and execution memories
    +-- write memory_diff.json and telemetry
    `-- mark the task completed
```

The client always awaits the POST response. When `wait=true`, it then polls the returned task ID
until Phase 2 completes, fails, or times out. When `wait=false`, it returns the accepted Phase 1
result immediately.

This gives terminal hooks the required durability boundary without making `/new`, `/reset`, or
gateway shutdown wait for LLM-based memory extraction.

Relevant sources:

- [OpenClaw client commit polling](https://github.com/volcengine/OpenViking/blob/6a14bc3edbad0c907125571ae8803d49feb22b12/examples/openclaw-plugin/client.ts#L825-L893)
- [Session service two-phase contract](https://github.com/volcengine/OpenViking/blob/6a14bc3edbad0c907125571ae8803d49feb22b12/openviking/service/session_service.py#L289-L314)
- [Phase 1 archive and queue creation](https://github.com/volcengine/OpenViking/blob/6a14bc3edbad0c907125571ae8803d49feb22b12/openviking/session/session.py#L1097-L1234)

## 6. Concurrent lifecycle events

**The plugin deduplicates overlapping `before_reset` and `session_end` hooks by canonical
OpenViking session ID, avoiding duplicate client requests without merging distinct sessions.**

The hook stores:

```ts
Map<ovSessionId, Promise<boolean>>
```

The canonical OpenViking ID matters because a raw OpenClaw session ID alone is not the full routing
identity. Two events may have different raw IDs but resolve to the same OpenViking session, or share
a raw ID while their session keys resolve to different OpenViking sessions.

The overlap behavior is:

```text
before_reset starts commit C1
    |
session_end resolves to the same OV session
    |
    `-- await C1 instead of starting C2

C1 succeeds -> both hooks finish
C1 fails    -> waiting hook retries after cleanup
```

OpenViking's server-side path lock remains the underlying cross-worker correctness boundary. The
plugin map reduces redundant terminal requests and gives failures a controlled retry path inside
one OpenClaw process.

The PR does not place periodic threshold commits in this same client-side map. If a periodic and
terminal request overlap, the server path lock serializes their Phase 1 archive operations and
rechecks the live messages while holding the lock.

## 7. Failure handling

**The promise chain converts synchronous throws, rejected promises, and explicit `false` results
into a reusable failure state, then clears only the matching in-flight entry.**

The call is deferred through `Promise.resolve().then(...)`:

```ts
handledCommit = Promise.resolve()
  .then(() => contextEngine.commitOVSession(...))
  .catch(() => false)
  .finally(() => {
    if (inFlightFinalCommits.get(ovSessionId) === handledCommit) {
      inFlightFinalCommits.delete(ovSessionId);
    }
  });
```

Deferring the call makes a synchronous throw behave like a rejected promise. The identity check in
`finally` prevents an older completion from deleting a newer promise installed for the same
session.

Retry behavior is deliberately bounded:

- A second terminal hook already waiting on a failed shared commit retries immediately.
- A later lifecycle event can retry because the map entry is cleared.
- A single failed terminal event with no later event is not persisted for startup recovery.

## 8. Option forwarding

**The PR extends the context-engine seam instead of hard-coding terminal behavior in the generic
commit helper, preserving existing callers while allowing lifecycle hooks to request a fast final
commit.**

The options travel through:

```text
lifecycle hook
    -> ContextEngineWithCommit.commitOVSession(options)
    -> commitOpenVikingSession(commitOptions)
    -> OpenVikingClient.commitSession(options)
    -> POST /api/v1/sessions/<id>/commit
```

The lifecycle service defaults remain:

```ts
wait: commitOptions?.wait ?? true
keepRecentCount: commitOptions?.keepRecentCount ?? 0
```

Existing callers that omit options therefore retain the old `wait=true` behavior. Only terminal
hooks explicitly select the non-blocking Phase 2 path.

## 9. Real example: committing on gateway restart

**A July 23, 2026 local gateway-restart probe shows the complete workflow: two live messages were
archived, the live session was drained, and asynchronous Phase 2 completed after the gateway had
already started shutting down.**

The probe used this OpenViking session:

```text
131adae2-c334-4e90-8bc1-c6b942e4c960
```

Its state before the restart was:

| Field | Value |
|---|---:|
| Trigger | Gateway restart |
| Live messages | 2 |
| Pending tokens | Greater than 0 |
| Commit count | 0 |

Restarting the gateway produced this sequence:

```text
gateway restart
    |
    v
OpenClaw emits session_end
    |
    v
plugin calls commitOVSession({ wait: false, keepRecentCount: 0 })
    |
    v
Phase 1 creates archive_001 and drains the live message file
    |
    v
POST returns; gateway shutdown can continue
    |
    `-- Phase 2 extracts memory asynchronously
```

The plugin recorded the successful lifecycle action:

```text
openviking: committed OV session on session_end for session=131adae2-c334-4e90-8bc1-c6b942e4c960
```

The observed state after the restart was:

| Field | Value |
|---|---:|
| Live messages | 0 |
| Pending tokens | 0 |
| Commit count | 1 |
| Root `messages.jsonl` size | 0 bytes |
| `archive_001` messages | 2 |
| Phase 2 task | Completed |

`archive_001` contained the original user and assistant messages. The extraction result was
`memories_extracted: {}` because this synthetic exchange contained no durable long-term fact. That
empty result does not mean the commit failed: the archive existed, the live file was empty, the
commit counter advanced, and the Phase 2 task reached completion.

This example also shows why `wait=false` is appropriate for shutdown. The gateway waited for the
short Phase 1 durability boundary, not the slower extraction work, while `keepRecentCount=0`
ensured that no terminal messages remained stranded in the live session.

## 10. Verification recorded by the PR

**The PR reports unit, integration, type-check, build, and live-runtime evidence for the terminal
paths. These are results recorded in the PR, not tests rerun while writing this note.**

Recorded checks:

| Check | Recorded result |
|---|---:|
| Focused lifecycle/context tests | 65 passed |
| Plugin suite excluding an unrelated architecture-boundary file | 671 passed |
| TypeScript type-check | Passed |
| Plugin build | Passed |
| Full plugin suite | 743 passed, 4 pre-existing architecture-boundary failures |

The live probes covered `/new`, `/reset`, the Webchat `sessions.reset` gateway operation, and
gateway restart. Each probe changed:

```text
2 live messages / commit_count=0 / pending_tokens>0
    ->
0 live messages / commit_count=1 / pending_tokens=0
```

Each generated `archive_001` contained the original user and assistant messages, and each
asynchronous Phase 2 task completed.

## 11. Scope and remaining limits

**The PR guarantees a final drain for lifecycle events that reach the plugin, but it is not a
general crash-recovery system.**

Covered:

- `session_end`
- `before_reset`
- overlapping terminal-hook deduplication
- bypassed sessions
- missing session/context-engine guards
- synchronous throws, rejected commits, and `false` results
- retries from a waiting or later lifecycle event

Not covered:

- `kill -9`, machine power loss, or a crash before Phase 1 returns
- a persistent client-side dirty-session journal
- a startup orphan-session sweeper
- automatic retry when one terminal event fails and no later event occurs
- synchronous confirmation that Phase 2 memory extraction succeeded

The key guarantee is narrower and precise: after a successful terminal commit POST returns, all
remaining live messages have crossed the Phase 1 archive boundary and Phase 2 has been queued.

## Source map

**The hook owns terminal orchestration, the context engine forwards policy, and the OpenViking
server owns durable archive correctness.**

| Source | Responsibility |
|---|---|
| [PR #3482](https://github.com/volcengine/OpenViking/pull/3482) | Change description, review, and recorded verification |
| [`openviking-lifecycle-hooks.ts`](https://github.com/volcengine/OpenViking/blob/6a14bc3edbad0c907125571ae8803d49feb22b12/examples/openclaw-plugin/plugin/openviking-lifecycle-hooks.ts) | Terminal hooks, deduplication, retry, cleanup |
| [`index.ts`](https://github.com/volcengine/OpenViking/blob/6a14bc3edbad0c907125571ae8803d49feb22b12/examples/openclaw-plugin/index.ts#L369-L389) | Dependency wiring and OV identity resolver |
| [`context-engine.ts`](https://github.com/volcengine/OpenViking/blob/6a14bc3edbad0c907125571ae8803d49feb22b12/examples/openclaw-plugin/context-engine.ts#L98-L105) | Public commit option seam |
| [`context-lifecycle-service.ts`](https://github.com/volcengine/OpenViking/blob/6a14bc3edbad0c907125571ae8803d49feb22b12/examples/openclaw-plugin/services/context-lifecycle-service.ts#L349-L390) | Identity resolution, client call, defaults, result handling |
| [`client.ts`](https://github.com/volcengine/OpenViking/blob/6a14bc3edbad0c907125571ae8803d49feb22b12/examples/openclaw-plugin/client.ts#L825-L893) | Commit POST and optional Phase 2 polling |
| [`session.py`](https://github.com/volcengine/OpenViking/blob/6a14bc3edbad0c907125571ae8803d49feb22b12/openviking/session/session.py#L1057-L1234) | Locked Phase 1 archive and persistent Phase 2 enqueue |
| [`openviking-lifecycle-hooks.test.ts`](https://github.com/volcengine/OpenViking/blob/6a14bc3edbad0c907125571ae8803d49feb22b12/examples/openclaw-plugin/tests/ut/openviking-lifecycle-hooks.test.ts) | Lifecycle, identity, overlap, failure, and retry coverage |

## Related learning notes

**The context-management note complements this explanation with the periodic threshold-commit
workflow.**

- [Context management](./context-management.md)
