# How OpenViking Data Is Used During Recall

> One-sentence conclusion: recall is the umbrella concept; use `find` for direct
> semantic discovery, use `search` for session-aware discovery or server-assembled
> context, and read L2 bodies only when the answer needs leaf content.

## Key Terms

Recall means recovering relevant stored context for a current task. In this broad
sense, both `find` and `search` are recall; the deprecated `/recall` endpoint is only
one legacy API with that name, not the definition of recall.

- **Recall:** the end-to-end act of bringing stored information back into use.
- **`find`:** direct semantic discovery from the caller's query or image.
- **`search`:** the unified API for ranked discovery (`mode="list"`) or
  injection-ready context assembly (`mode="context"`).
- **URI-addressable semantic node:** one stored file or directory that callers can
  name and inspect through a Viking URI. A source document may become several stored
  section files, each with its own URI.
- **Index record:** one vector-index representation of a semantic node at a numeric
  level. Directory L0 and L1 records may share the same raw URI; an L2 record points
  to a leaf file.
- **Matched hit:** one caller-visible `MatchedContext` projected from an index record.
  It carries `context_type`, `uri`, `level`, `score`, indexed `abstract`, and `tags`,
  but not the vector or a separately hydrated body.
- **Intent analysis:** an optional pre-retrieval model call that turns the current
  query plus session context into one or more typed retrieval queries; it does not
  rerank results.
- **`/recall`:** a deprecated compatibility preset over context assembly; use
  `/search` with `mode="context"` for new integrations.

## TL;DR

These are recommended-contract guidelines for any consumer or agent integrating with
OpenViking — not a description of the VikingBot runtime (see §Scope and Verification
Boundary).

OpenViking does not execute a fixed “read L0, then L1, then L2” pipeline. It searches
URI-addressable semantic nodes through their index records, may use directory L0/L1
records to reach L2 leaves, and only reads stored bodies when context assembly or the
caller asks for more detail.

- **Choose the recall interface deliberately:** `find` and `search` both recall
  stored context. Use `find` when the caller owns scope, ranking consumption, and
  content reads; use `search` when session-aware planning or server-side assembly is
  valuable. See §1 for the exact difference and three in-repository flows.
- **Treat recall granularity precisely:** one public list hit identifies one stored
  URI-addressable node, not necessarily one original source document. Default resource
  parsing can persist several structural or size-bounded section files, while a normal
  memory leaf is recalled as one L2 file URI. See §4.
- **At session start:** read `viking://user/<user_id>/memories/profile.md` once and
  inject it as stable user context. Context recall intentionally filters this file.
- **On each turn (general agents):** use `POST /api/v1/search/search` with
  `mode="list"` (or `/find`) and read bodies explicitly only when needed. Reserve
  `mode="context"` for when you want OV to produce a ready-to-inject, token-bounded
  block — it is heavier (query expansion + selective body reads + tier budgeting +
  optional rewrite/digest), and the Go SDK does not expose it at all.
- **For future action guidance:** prefer `experiences`, which has a normal recall quota.
- **For historical execution evidence:** target
  `viking://user/<user_id>/memories/trajectories` with `find`, select an L2 hit, and
  `read` it when the indexed result is not sufficient.
- **Do not combine the level systems:** list search uses numeric index levels
  `0/1/2`; context assembly uses content detail tiers
  `uri/abstract/overview/full`.

## Scope and Verification Boundary

This document describes source-confirmed behavior at commit
`c98aec50d002026d756c748da23c7cf7b6b605c5`. Runtime features such as sparse vectors,
reranking, memory extraction, and Agent Evolution remain configuration-dependent.
The deployment profile used in this document intentionally leaves `rerank`
unconfigured to avoid its additional latency and model cost; the recommended
`find` and list-mode `search` flows therefore use QUICK vector retrieval.

The code and focused mocked-boundary tests were exercised; no live server, model,
vector database, or user data was exercised for this document. Sections labelled
**Recommended consumer contract** describe integration guidance, not an automatic OV
runtime guarantee. The local profile statement below is derived from the active
server command and redacted resolved configuration; no recall endpoint or model was
invoked to validate retrieval quality.

## 1. Recall Is the Umbrella: `find` vs `search`

`find` and `search` are both recall operations. The difference is ownership:
`find` gives the caller direct ranked discovery, while `search` can add session-aware
query planning and, in context mode, own the complete assembly step.

```text
recall (broad concept)
  |
  +-- find
  |     `-- caller query -> URI-deduplicated ranked hits
  |
  `-- search
        +-- mode="list"    -> optional session-aware plan -> ranked hits
        `-- mode="context" -> retrieval + selective reads + budgeted context block

/recall (deprecated endpoint) -> compatibility preset for search(mode="context")
```

| Difference | `find` | `search(mode="list")` | `search(mode="context")` |
|---|---|---|---|
| Query handling | Uses the caller's raw text or image as one typed query | Uses the raw query when session context is absent, intent analysis is disabled, or the query is an image; otherwise it may turn session context into multiple typed queries | May expand the query from the session before retrieval |
| Retrieval | Runs one QUICK `HierarchicalRetriever` request in this no-rerank profile | Runs the same QUICK retriever once per typed query and aggregates the results | Calls scoped `find` operations, then deduplicates and plans the context block |
| Result | Ranked `memories`/`resources`/`skills` hits | The same ranked buckets, plus an optional query plan and provenance | `entries`, rendered context, optional digest, and assembly statistics |
| Body reads | None | None | Selective reads when the chosen detail tier needs stored content |
| Scope controls | Supports `target_uri` and `level` | Supports `target_uri` and `level` | Rejects `target_uri`; ignores `level` because `detail` controls content depth |
| Best fit | The caller knows the scope and owns post-processing | The caller wants ranked hits but may benefit from session-aware intent planning | The caller wants OpenViking to return a token-bounded block ready for injection |

Raw query does not mean a primitive vector-database lookup. `find` still uses the
hierarchical retriever with dense and, when the embedder provides them, sparse query
vectors. This deployment does not configure a rerank provider, so the retriever
chooses QUICK and never calls a reranking model. Conversely, `search(mode="list")`
is not automatically heavier: with no usable session context it sends the raw query
through that same QUICK retrieval core.

### What `find` returns

`find` returns ranked references to stored semantic nodes, not an answer and not a
guaranteed full body. The HTTP endpoint wraps the result below in
`{"status":"ok","result":...}`; the current Python SDK returns the inner `result`
dictionary.

```json
{
  "memories": [
    {
      "context_type": "memory",
      "uri": "viking://user/default/peers/sender-1/memories/events/e1.md",
      "level": 2,
      "score": 0.9,
      "abstract": "short event",
      "tags": ["source=chat"]
    }
  ],
  "resources": [],
  "skills": [],
  "total": 1
}
```

The three arrays classify the hit by context type, and `total` counts all three
arrays. `uri` is both the node identity exposed to the caller and the handle for a
later content read. `level` says whether the match represents directory L0, directory
L1, or an L2 leaf. `score` orders relevance; it is not a probability or a factual
confidence score. `abstract` is the indexed scalar and may be sufficient for a small
memory, but it is not a general full-content contract. The response omits the vector,
internal record ID, and separately hydrated `content`.

Within one retriever invocation, `find` groups vector candidates by raw URI and keeps
the highest-scored candidate before projecting the public hits. If scores are equal,
the earlier candidate remains. This is distinct from any later deduplication performed
by a consumer or by list-mode `search` across multiple typed queries.

### Real flow 1: VikingBot owns recall with `find`

VikingBot uses `find` for its per-turn, type-quota memory recall path because the
bot itself owns the category limits, peer fan-out, content budget, and prompt format.

```text
current user message
  -> VikingBot sets quotas for events, entities, and preferences
  -> for each peer and memory type:
       find(query=current_message, target_uri=type_directory, limit=type_quota)
  -> each find returns the highest-scored hit for each raw URI
  -> VikingBot merges those calls, drops empty or already-seen returned URIs
     (the first cross-call occurrence wins), then sorts and applies the type quota
  -> VikingBot drops /profile.md because profiles are read and injected separately
  -> VikingBot reads candidate bodies, applies per-type character budgets,
     skips duplicate content, and falls back to summaries or URI-only entries
     when content does not fit
  -> a formatted "user memories" message is placed immediately before the
     unchanged current user message
```

The shorthand above contains four separate filters:

1. **Retriever deduplication by raw URI.** QUICK and THINKING retrieval both collect
   candidates by the raw index URI and retain the highest score before `find` returns.
   This can collapse directory L0 and L1 records that share one raw URI; §4 explains
   why those are still distinct index records.
2. **VikingBot aggregation deduplication.** `_dedupe_memories` walks returned hits in
   order, discards hits whose display URI is empty, and keeps only the first hit for
   each exact display URI. It does not compare duplicate scores. The type-quota path
   invokes this helper while merging peer and category calls, then sorts the retained
   hits by score and applies each type's quota.
3. **Explicit profile exclusion.** After all type/peer results are merged,
   `get_viking_memory_context` removes a hit when
   `uri.rstrip("/").endswith("/profile.md")` is true. This is a case-sensitive URI
   suffix check performed before the memory block is formatted; it does not inspect
   the file body. VikingBot separately reads each relevant peer profile into the
   system prompt, so this exclusion prevents duplicate baseline context and quota
   competition. It is unconditional if that direct read fails. In the normal
   type-quota flow, category-scoped targets already make a profile hit unlikely; the
   filter also protects the broader legacy path.
4. **Content deduplication during formatting.** VikingBot reads each remaining
   candidate and computes an in-process hash of `content`, falling back to `abstract`
   and then `uri`. If that hash was already seen in the current formatting pass, the
   later candidate is omitted even when it has a different URI.

The following executable fixture shows how ranked hits become prompt data. Its query
is deliberately generic so the fixture tests assembly rather than retrieval quality:

```text
find("hello", target_uri=.../events/, limit=10)
find("hello", target_uri=.../entities/, limit=10)
find("hello", target_uri=.../preferences/, limit=3)

returned candidates after per-type ranking
  events:      e1.md 0.9, e2.md 0.8, e3.md 0.7
  entities:    en1.md 0.9, en2.md 0.8
  preferences: p1.md 0.9, p2.md 0.8
```

VikingBot reads every remaining candidate body before deciding how much of it fits.
With the fixture's 1,100-character budget, the output contains three full entries,
one event summary, and three URI-only entries. Representative entries are:

```xml
<memory_group type="events">
  <memory index="1" type="full">
    <uri>viking://user/default/peers/sender-1/memories/events/e1.md</uri>
    <score>0.9</score>
    <content>short event</content>
  </memory>
  <memory index="2" type="summary">
    <uri>viking://user/default/peers/sender-1/memories/events/e2.md</uri>
    <score>0.8</score>
    <summary>long event summary</summary>
  </memory>
</memory_group>
<memory_group type="entities">
  <memory index="5" type="uri">
    <uri>viking://user/default/peers/sender-1/memories/entities/en2.md</uri>
    <score>0.8</score>
  </memory>
</memory_group>
```

`full` means the body was read and fit the category budget. `summary` means an event
body did not fit and VikingBot extracted its `Summary:` field. `uri` keeps a ranked
pointer when the body does not fit; the model can use `openviking_multi_read` if that
candidate becomes necessary.

VikingBot does not concatenate this XML into the user's original string. It sends a
separate user-role context message followed by the unchanged current message:

```text
role=system  -> baseline instructions + directly read peer profile
role=user    -> current time/session + formatted OpenViking memory block
role=user    -> the actual current user message
```

This is genuine recall even though the API call is named `find`: OpenViking owns
semantic discovery, while VikingBot owns recall policy and assembly. The implemented
flow and its two deduplication helpers are in
[`_dedupe_memories`](../../../bot/vikingbot/agent/memory.py#L144-L153),
[`_parse_viking_memory`](../../../bot/vikingbot/agent/memory.py#L287-L363),
[`_search_viking_memory_by_type_quota`](../../../bot/vikingbot/agent/memory.py#L427-L473)
and
[`get_viking_memory_context`](../../../bot/vikingbot/agent/memory.py#L532-L650); the
context builder constructs the two user-role messages in
[`context.py`](../../../bot/vikingbot/agent/context.py#L427-L441). The executable
fixture is
[`test_viking_memory_type_quota_groups_with_event_summaries_and_uris`](../../../bot/tests/test_openviking_api_key_type.py#L2262-L2374).

### Real flow 2: the Bob demo uses `search(mode="list")` for a follow-up

The cloud Bob demo puts `find` and list-mode `search` side by side. It uses `find`
for a self-contained question, then passes a committed session to `search` for the
ambiguous follow-up “what else should I pay attention to?”

```text
session records local setup and test instructions
  -> commit session and wait for memory processing
  -> find("local development environment setup steps", limit=3)
       -> raw query -> one typed query -> ranked hits
  -> search("what else should I pay attention to?", session_id=..., limit=3)
       -> Python SDK omits mode, so the server defaults to mode="list"
       -> when intent analysis is enabled, load the session
       -> combine the follow-up with available current messages/archive overview
       -> produce one or more typed queries
       -> run the same HierarchicalRetriever for each typed query
       -> aggregate ranked memories/resources/skills
  -> Bob prints abstracts and URIs from the list response
```

This is the central `find` versus `search(mode="list")` difference: both return
ranked hits without body hydration, but list-mode `search` may first resolve
an underspecified follow-up against session context. It does not budget content or
return `rendered`/`digest`; those belong to `mode="context"`. If intent analysis is
disabled or the session supplies no usable context, list-mode `search` falls back to
the raw query and becomes close to `find` over the same retrieval core.

The paired calls are implemented in
[`examples/cloud/bob.py`](../../../examples/cloud/bob.py#L125-L180). The server's
list-mode route loads the session conditionally in
[`search.py`](../../../openviking/server/routers/search.py#L369-L425), and the
session-aware query plan and aggregation live in
[`VikingFS.search`](../../../openviking/storage/viking_fs/_semantic.py#L302-L435).

#### If intent analysis is disabled, is list-mode `search` equal to `find`?

They are retrieval-equivalent for the normal ranked-hit response, but they are
not the same API operation. With identical query, scope, filters, limits, level,
threshold, tenant, and image input, and with this deployment's no-rerank profile,
both create one raw typed query and run it through the same QUICK retriever. The
default HTTP payload should therefore contain the same ranked
`memories`/`resources`/`skills` records in the same order.

The remaining differences are observable:

- The endpoints, request schemas, and telemetry operation names remain different:
  `search.find` versus `search.search`.
- A disabled intent flag makes `search` skip both session loading and session-context
  collection, even when the caller still supplies `session_id`.
- When `target_uri` is present, the current `search` implementation still attempts
  to read the first target directory's abstract before selecting the raw-query
  branch. The value does not change retrieval when intent is disabled, but the extra
  read does not occur in `find`.
- `search` retains its single `QueryResult`, so `include_provenance=true` can expose
  per-query provenance. `find` discards that internal `QueryResult` while building
  its public `FindResult`, so the same flag has no provenance payload there.

`QueryResult` is only the diagnostic envelope for one retriever invocation. It holds
the typed query, the URI-deduplicated matched hits later copied into the public
buckets, the directories searched, and a retrieval trace. `find` does not discard an
additional hit during this conversion: the retriever has already collapsed raw-URI
duplicates before `VikingFS.find` copies `QueryResult.matched_contexts` into
`memories`/`resources`/`skills` and drops only the envelope. `search` copies the same
hits and also stores the envelope in `FindResult.query_results`.

The final caller-visible effect is therefore:

| Request | `find` response | `search(mode="list")` response | Retrieval effect |
|---|---|---|---|
| `include_provenance=false` | Ranked buckets + `total` | The same ranked buckets + `total`; `query_plan` appears only when intent ran | None |
| `include_provenance=true` | Still no `provenance` key | Adds one `provenance[]` item per typed query | None; serialization happens after retrieval |

Each search provenance item contains the executed query text, searched directories,
per-query matched URI/tier/type/score/match reason, and the retrieval trace with its
statistics. With intent disabled there is one item for the one raw query; with intent
enabled there can be several, which shows which expanded query produced each hit.
This is useful for explaining duplicates, a `total` larger than `limit`, or unexpected
scope. It does not change embeddings, vector searches, thresholds, result order,
result count, model calls, or reranking cost.

The tradeoff is a larger response that exposes internal query and directory metadata.
Also, the current Python, Go, and TypeScript SDK option types do not expose
`include_provenance`; callers need a direct HTTP request to use it. For ordinary
recall, leave it false. For retrieval debugging with intent disabled, list-mode
`search` can expose this diagnostic envelope while `find` cannot.

For all three current SDKs, the effective default is therefore `false`: the SDK omits
`include_provenance` from the request body, the server applies
`SearchRequest.include_provenance=false`, and the response omits the `provenance` key
entirely. It is not returned as `null` or an empty list. The typed SDK methods also
cannot opt into `true`; use a raw HTTP request when this diagnostic payload is needed.

Here “provenance” means **retrieval-execution provenance**: which typed query ran,
which directories it searched, which hits that query matched, and what trace the
retriever recorded. It does not mean content lineage, ingestion source, document
authorship, or a citation proving that a claim came from a particular source file.

The following is an abridged shape example, not a live result:

```text
POST /api/v1/search/search
{
  "query": "OAuth best practices",
  "target_uri": "viking://resources/docs",
  "mode": "list",
  "limit": 2,
  "include_provenance": true
}

result
  memories:  [...normal ranked hits...]
  resources: [...normal ranked hits...]
  skills:    []
  total:     <normal result count>
  provenance:
    - query: "OAuth best practices"
      searched_directories:
        - "viking://resources/docs"
      matched_contexts:
        - uri: "viking://resources/docs/oauth.md"
          tier: "L2"
          context_type: "resource"
          score: <vector score>
          match_reason: <retriever-generated reason>
      thinking_trace:
        events: [...structured retrieval events...]
        statistics: {...trace counts and duration...}
```

The same call through a current typed SDK omits `include_provenance`, so the
`provenance` block above is absent while the ordinary ranked buckets and `total`
remain.

Therefore, use “same retrieval path/results under equal inputs,” not strict
`find == search(mode="list")`. If intent is intentionally disabled and no search-only
provenance is needed, `find` is the simpler interface; with `target_uri`, it also
avoids the extra abstract read described above.

#### Complete `search(mode="list")` request options

List mode accepts the following request-body fields. Unknown fields are rejected;
the context-assembly fields listed afterward are also rejected when `mode="list"`.

| Field | Type and default | Effect in list mode |
|---|---|---|
| `mode` | `"list"` (default) | Selects ranked-hit output rather than context assembly. The Python SDK omits this field because list is the server default. |
| `query` | string, `""` | Text query. It may be empty only when `image_url` is present. |
| `image_url` | string or `null` | Data URI, HTTP(S) URL, or `viking://` image URI. Requires a multimodal embedder, forces direct QUICK retrieval, defaults `level` to L2 when omitted, and skips session intent analysis. |
| `session_id` | string or `null` | Makes intent analysis possible. With intent disabled it is not loaded; for an image query it may be loaded by the route but is not collected or sent to the planner; empty session context also skips planning. |
| `target_uri` | string or list, `""` | Restricts retrieval to one or more directories. With intent analysis, the first explicit target's abstract is also optional planning context. |
| `context_type` | `memory`, `resource`, `skill`, or a list | Adds a result-type filter. Comma-separated strings are accepted and normalized. |
| `limit` | integer, `10` | Maximum results **per typed query**, not a final aggregate cap. Multiple planned queries can therefore return more than `limit` total hits. |
| `node_limit` | integer or `null` | Compatibility alias that takes precedence over `limit` when supplied. |
| `score_threshold` | number or `null` | Overrides the retriever threshold. When omitted in the current no-rerank profile, the configured/default threshold is `0.1`; that threshold filters vector scores and does not call a reranker. |
| `level` | `0`, `1`, `2`, or a list/string form | Restricts returned index levels: L0 abstract, L1 overview, or L2 leaf. In QUICK mode it is pushed into the vector query. |
| `filter` | object or `null` | Raw backend metadata-filter DSL. It is AND-combined with `context_type`, time bounds, and tags; backend support for custom predicates can vary. |
| `tags` | list of strict `k=v` strings or `null` | Normalizes tags to lowercase and requires every requested tag. Repeated keys keep the last supplied value. |
| `since` | relative/ISO time string or `null` | Inclusive lower bound such as `2h`, `2026-08-01`, or an ISO timestamp. |
| `until` | relative/ISO time string or `null` | Inclusive upper bound. The request fails when `since` is later than `until`. |
| `time_field` | `updated_at` or `created_at`; default `updated_at` | Selects the metadata field used by `since`/`until`. |
| `include_provenance` | boolean, `false` | HTTP-only in the current SDKs. For list-mode `search`, adds per-typed-query provenance, searched directories, and thinking traces without changing retrieval. `find` accepts the field but emits no provenance; a generated search `query_plan` is independent of this flag. |
| `telemetry` | boolean or `{ "summary": boolean }`; default `false` | Requests an operation telemetry summary in the HTTP response. |

These context-only fields are invalid in list mode:
`query_expansion`, `max_tokens`, `quotas`, `purpose`, `detail`, `dedup_turns`,
`exclude_uris`, `peer_scope`, `other_peer_penalty`, `rewrite`, and
`rewrite_max_bullets`. They belong only to `search(mode="context")`; list-mode intent
is controlled globally by `retrieval.enable_intent`, not per request by
`query_expansion`.

List-mode aggregation has two non-obvious consequences. It appends each typed
query's results into the `memories`, `resources`, and `skills` buckets without a
final global sort or cross-query URI deduplication. Therefore, multiple typed queries
can produce duplicate URIs and a `total` greater than `limit`; consumers that need a
single globally ranked list must normalize it themselves.

### Real flow 3: the Claude Code memory plugin delegates recall to `search`

The Claude Code memory plugin uses `search(mode="context")` because it wants the
server to apply the coding preset, session-aware expansion, deduplication, detail
tiers, and token budgeting before returning text for prompt injection.

```text
current coding query + optional session ID
  -> plugin posts search(mode="context", purpose="coding", ...)
  -> OpenViking optionally expands the query from session context
  -> context assembler runs one or more scoped find operations
  -> profile/exclusion/peer filters and URI dedup run
  -> selected bodies are read only when their detail tier requires content
  -> token budgeting produces entries + rendered context + optional digest + stats
  -> plugin injects digest when available, otherwise rendered context
```

Here the application delegates both discovery and assembly to OpenViking. The plugin
builds and sends the request in
[`buildContextSearchBody`](../../../examples/claude-code-memory-plugin/scripts/shared/recall-core.mjs#L92-L127)
and
[`fetchAssembledContext`](../../../examples/claude-code-memory-plugin/scripts/shared/recall-core.mjs#L450-L486).
If the context face is unavailable, it falls back to the deprecated `/recall` preset
and then to client-side `find`; those fallbacks are compatibility paths, not a change
to the broad meaning of recall.

## 2. Intent Analysis Is Context-Aware Query Planning

In the current implementation, “intent analysis” does one narrow job: it uses recent
session context to expand or restate an underspecified current message as zero or more
explicit typed queries. The name does not imply a second retrieval algorithm; this
step does not score, rerank, hydrate, or rewrite retrieved results.

### Where the context comes from

OpenViking knows the context only because the caller supplies `session_id`. The
server resolves that session inside the authenticated account/user scope, loads it,
and builds a bounded planning view; it does not infer context from all stored memories
or from another session.

```text
caller sends query + session_id
  -> router resolves Session(request_account, request_user, session_id)
  -> load the session
  -> find the newest archive marked completed or failed
       completed -> use its readable .overview.md
       failed    -> use no archive overview
  -> collect raw messages from newer pending archives
  -> append the session's current live messages
  -> stable-deduplicate the merged message list
  -> keep at most the latest 20 messages for search context
  -> IntentAnalyzer keeps only the latest 5 for its prompt
  -> add the current query and optional first target_uri abstract
  -> call query planner / VLM
```

The latest completed archive overview carries the older conversational working
memory; the recent message slice carries turns newer than the latest completed/failed
archive boundary plus the active session messages. The current query is passed
separately—it is not chosen from history. If `session_id` is absent, intent is
disabled, or both the overview and message slice are empty, no planning-model call
occurs and `search` uses the raw query.

### Inputs and output

The planner sees only a bounded session view and produces a structured `QueryPlan`.
It does not receive the entire OpenViking store or the bodies of candidate hits.

| Planner input | Source | Bound or behavior |
|---|---|---|
| Current query | `search.query` | Passed as the current message in the planning prompt |
| Latest archive overview | Newest archive marked completed or failed | Uses the overview only when that newest marked archive completed successfully and its overview is readable |
| Recent current messages | Messages from newer pending archives plus the live session | Stable-deduplicated; search keeps at most 20, then `IntentAnalyzer` uses only the last 5 |
| Target abstract | First explicit `target_uri` | Best-effort read; empty when no target is supplied or the read fails |

The model response is parsed as:

```text
QueryPlan
  reasoning
  queries[]
    query          explicit semantic query text
    context_type   memory | resource | skill
    intent         explanation metadata
    priority       planning metadata
```

`context_type` constrains each typed retrieval. The current list implementation does
not globally reorder queries or final hits by `priority`: it launches the typed
queries concurrently, preserves query-list order when aggregating their results, and
appends hits into the three result buckets.

### Runtime flow and trigger conditions

Intent analysis runs only for a text query with an enabled flag, a supplied session,
and usable session context. Every other branch uses the caller's raw query directly.
An image request with `session_id` may still load the session at the HTTP route, but
it skips session-context collection and the planning-model call.

```text
search(mode="list")
  |
  +-- image_url present ------------------------------> raw image typed query
  |
  +-- enable_intent=false ----------------------------> raw text typed query
  |
  +-- no session_id ----------------------------------> raw text typed query
  |
  +-- session has no messages and no archive overview -> raw text typed query
  |
  `-- text + enabled + session context
        -> one query-planner/VLM call
        -> QueryPlan with zero or more typed queries
        -> one embedding + QUICK vector search per typed query
        -> append ranked hits; no final rerank or cross-query dedup
```

The flag is necessary but not sufficient. All four conditions below must hold for
the model call:

1. `retrieval.enable_intent` is `true`.
2. The request includes `session_id`.
3. The loaded session has at least one current message or a latest completed archive
   overview.
4. The request is text retrieval rather than `image_url` retrieval.

Planner/provider errors and invalid JSON fail the request; there is no silent raw-query
fallback after the planner is invoked. A valid plan with an empty `queries` array
performs no vector retrieval and returns empty ranked buckets.

### Model selection, latency, and cost

Intent analysis costs one generation call before retrieval. OpenViking uses the
dedicated `query_planner` configuration when present and otherwise falls back to
`vlm`; this model role is independent of result reranking.

If the plan contains `N` typed queries, list mode then performs `N` embedding calls
and `N` QUICK vector searches. Therefore, enabling intent can increase latency and
model/embedding cost even though no reranking model is configured. It does not cause
body reads, context token budgeting, digest rewriting, or any rerank call.

For the cost-sensitive consumer contract in this document:

- Use `find` for a self-contained query that already says what to retrieve.
- Use `search(mode="list", session_id=...)` only when a follow-up depends on session
  context and the planning call is worth its cost.
- Leave `rerank` empty or omitted so all resulting typed queries stay on QUICK vector
  retrieval.

### Enabling intent analysis without enabling reranking

The two features are configured independently:

```json
{
  "retrieval": {
    "enable_intent": true
  },
  "rerank": {}
}
```

`retrieval.enable_intent` defaults to `true`, so omitting `retrieval` has the same
effective behavior; setting it explicitly makes the choice visible. An empty or
omitted `rerank` section creates no rerank client. Restart the server after changing
these server settings.

The current local server resolves `~/.openviking/ov.conf`. That file omits
`retrieval`, `rerank`, and `query_planner`, so intent analysis is enabled by default,
retrieval uses QUICK with no reranking model, and the intent role falls back to the
configured VLM.

## 3. What Commit Produces

Commit separates conversation preservation from reusable-memory extraction. Session
archive L0/L1 helps later query understanding, while profile, event, entity,
preference, experience, and trajectory files are long-term L2 memory leaves.

```text
session.commit()
  |
  +-- archive messages
  |     +-- archive_N/.abstract.md       session L0
  |     `-- archive_N/.overview.md       session L1 / working memory
  |
  `-- asynchronous memory extraction
        +-- memories/profile.md          stable user facts, L2 leaf
        +-- memories/{events,...}/*.md   reusable memories, L2 leaves
        `-- memories/trajectories/*.md   execution contracts, L2 leaves
```

The current V3 extraction path writes `profile.md` by patching user-confirmed stable
facts. Trajectory creation is conditional on extraction policy and configuration;
Agent Evolution is disabled by default at the server-account level. A trajectory is
append-only and contains a reusable operation contract rather than a raw transcript.

Memory leaves are indexed as `level=2`. For a normal memory file, the vector record's
`abstract` scalar is the link-stripped memory body. A schema may choose different
embedding text: trajectories embed only `trajectory_name` plus `retrieval_anchor`,
even though their indexed `abstract` still contains the operation contract.

## 4. The Two Level Systems

The storage/index level identifies what node was matched; the assembly detail tier
identifies how much text will be injected. They overlap in vocabulary but are not a
one-to-one mapping.

### A URI-addressable node is the public recall unit

One public list hit identifies one stored semantic node, but that node is not
necessarily the original source document. Default resource parsing preserves natural
heading structure and also splits oversized sections by paragraph and bounded size.
Each generated Markdown section is stored as a visible file with its own URI and is
indexed as an L2 leaf. For a long document with no headings, the stored layout can be:

```text
source: guide.md
  -> viking://resources/guide/guide_1.md   L2 node
  -> viking://resources/guide/guide_2.md   L2 node
  -> viking://resources/guide/guide_3.md   L2 node
```

`args.parse_mode="no_split"` instead converts each source document into one Markdown
body, so the same source normally produces one L2 file URI. Code resources also
preserve each source file without parser chunking. Memory recall follows the same
public rule at a different creation boundary: each persisted `events`, `entities`,
`preferences`, `profile`, `experience`, or `trajectory` file is one L2 candidate.

| Boundary | Smallest relevant unit |
|---|---|
| Original input | A source document, directory, media object, or memory update |
| Stored resource | One URI-addressable file or directory; a source document may produce several files |
| Vector index | Conceptually `(account, raw URI, level)`; directory L0/L1 can share the raw URI |
| Public `find` result | One `MatchedContext` hit with one display URI and one level; never an anonymous byte range |
| Consumer context | The URI pointer, indexed abstract, or explicitly read body chosen for injection |

This is why “OpenViking never saves chunks” is too broad. It does not expose opaque
vector-database chunk IDs as its public recall contract, but default document parsing
can materialize structural or size-bounded chunks as ordinary URI-addressable files.
Provider-side embedding input handling does not create additional caller-visible
hits unless ingestion created additional stored nodes.

| System | Value | Meaning | Does it require a content read? |
|---|---|---|---|
| Index | L0 / `level=0` | Directory `.abstract.md` vector record | No; text is in the vector payload |
| Index | L1 / `level=1` | Directory `.overview.md` vector record | No; overview is copied into the payload's `abstract` scalar |
| Index | L2 / `level=2` | Leaf file, including memory Markdown | List search does not read the hit body |
| Assembly | `uri` | Pointer only | No |
| Assembly | `abstract` | Candidate `abstract` scalar | No |
| Assembly | `overview` | Directory sidecar or a summary/skeleton derived from a leaf | Yes |
| Assembly | `full` | Full visible leaf content | Yes |

Three consequences matter:

1. An L2 memory served at assembly detail `abstract` may already expose its complete
   memory body because memory indexing stores that body in the scalar.
2. An L2 memory served at detail `overview` is usually read and compressed from that
   same leaf file; it is not necessarily a directory L1 `.overview.md` record.
3. A directory's L0 and L1 records have distinct index identities but can share one
   raw URI. Within one `find` invocation, the retriever keeps the higher-scored raw-URI
   candidate, then reconstructs `/.abstract.md` or `/.overview.md` on the returned
   display URI. A normal L2 file has one current record for its URI and level because
   subsequent vectorization upserts the same deterministic identity.

For resources, an L2 embedding can use full text, a generated summary, a filename, or
multimodal input depending on content type and embedding configuration. Therefore,
“L2 vector equals the complete original file” is not a valid general assumption.

## 5. How List Search Uses L0, L1, and L2

List search returns ranked `MatchedContext` hits projected from index records, not
hydrated bodies. Its use of the three levels changes according to whether a reranker
is available. The deployment profile used here has no reranker and therefore always
follows the QUICK branch on the left;
the THINKING branch is shown only to define the optional behavior if another
deployment configures one.

```text
find(query) ---------------------------- raw query
search(query, session_id) -------------- optional session-aware typed queries
                      |
                      v
             embed dense + optional sparse
                      |
          +-----------+-----------+
          |                       |
     QUICK mode                THINKING mode
     no reranker               reranker available
          |                       |
     one vector query          global L0/L1 search
     across levels             + scalar rerank
                                  |
                              recurse through
                              direct children
                                  |
                           L0/L1 continue; L2 stops
```

In QUICK mode, one tenant-scoped vector query searches all levels unless `level` is
specified. `level=[2]` is pushed directly into the vector query. Before applying the
public limit, QUICK retrieval groups candidates by raw URI and keeps the highest score;
THINKING retrieval applies the same raw-URI rule while collecting recursive results.

In THINKING mode, global search first selects L0/L1 directory entry points. It then
searches direct children and propagates scores; only non-L2 results continue the
recursion. Reranking uses each index record's `abstract` scalar and does not read the
corresponding AGFS file. A `level=[2]` filter limits returned candidates but may still
use L0/L1 internally as traversal routes.

The returned list item contains `uri`, `level`, `score`, indexed `abstract`, and tags.
Display URIs for L0/L1 reconstruct the `/.abstract.md` or `/.overview.md` suffix. To
obtain a selected L2 file's visible content, call `read(hit.uri)` explicitly.

### Why `mode="list"` is the general-agent default

`mode="list"` (and `/find`) returns ranked hits and performs **no body
hydration**. In this no-rerank profile, QUICK `/find` performs one dense (+ optional
sparse) vector query, while session-aware `/search` performs one QUICK vector query
per typed query produced by intent analysis; vector scores are final and no reranking
model is called. A different deployment that configures reranking would use THINKING
instead. The response groups hits under `memories`/`resources`/`skills` buckets with a
`total` and optional `query_plan`; each hit carries
`context_type`/`uri`/`level`/`score`/`abstract`/`tags`. Because a memory L2 record
already stores its link-stripped body in the `abstract` scalar (§4), a list hit
frequently carries the full memory text with zero extra reads. You call `/content/read`
only on the few hits whose `abstract` is insufficient. Both `/find` and `/search
mode="list"` construct the same `HierarchicalRetriever` (find at
`openviking/storage/viking_fs/_semantic.py:231-252`, search at `:328-406`), so both
use QUICK in this deployment; `/search` only adds optional session-aware intent
expansion on top.

`mode="context"` is materially heavier per turn: query expansion (an LLM/vector pass
over the current messages and the latest archive overview), named-bucket `find` calls,
selective body reads for candidates that can reach `overview`/`full`, breadth-first
tier allocation inside `max_tokens`, deepening higher-score entries with leftover
budget, an optional rewrite digest (another model call), and a cross-turn recall
ledger. That is the right tool when the client wants OV to own context assembly; it is
overkill for an agent that already has its own prompt builder and only needs ranked
hits. Real consumers split on which they use: the **Claude Code memory plugin prefers
`mode="context"` (`purpose="coding"`) as its primary recall path**, while the **Go SDK
and Python VikingBot expose or consume the lighter list path**. The Go SDK has no
`mode` field on `SearchOptions` and no `digest`/`rendered` response surface
(`sdk/go/retrieval.go`, `sdk/go/types.go:211`). Pick `mode="list"` when you assemble
context yourself or your SDK lacks the context face; pick `mode="context"` when you
want the server to budget, tier, and optionally rewrite-digest for you. See §1 for
the full VikingBot and Claude Code plugin flows.

## 6. How Context Search Uses the Retrieved Data

`mode="context"` is the heavier assembly face: it turns ranked hits into an
injection-ready, token-bounded block. It uses the same semantic index underneath but
performs selective reads only after candidate retrieval and ranking. Treat it as an
optional assembly service, not the default per-turn call (see §5).

```text
query + optional session_id
  -> query expansion from current messages + latest archive overview
  -> flat find, or named quota-bucket finds
  -> profile/exclusion/peer filtering and URI dedup
  -> read only candidates that can reach overview/full
  -> breadth-first tier allocation inside max_tokens
  -> deepen higher-score entries with remaining budget
  -> rendered <memory ...> block
  -> optional cited digest + cross-turn recall ledger
```

With `detail` omitted, current defaults are:

| Category | Start detail | May deepen automatically | Default body read? |
|---|---|---|---|
| `events` | `overview` | `full` | Yes |
| `entities`, `preferences`, `experiences` | `abstract` | No | No |
| `resources`, `skills` | `abstract` | No | No |
| catch-all `memories` such as trajectories/cases/tools | `abstract` | No | No |
| directory hit | `overview` | No | Reads its `.overview.md` |

The planner first gives candidates breadth at their starting tier, then spends
remaining tokens on higher-score candidates up to their ceiling. An oversized tier
falls back to a cheaper tier instead of truncating its text. Resources and skills
with missing or oversized abstracts fall back to a URI instead of silently reading a
possibly large or sensitive body.

Session archive L1 participates differently: the newest completed archive overview
can help expand the current query when `session_id` is supplied. Session namespaces
are not indexed by the normal resource indexer, so archive L0/L1 should not be treated
as ordinary recall hits.

## 7. API Choice

Choose the API by the result the agent needs: discovery, ready-to-inject context, an
exact text match, or complete content.

| API | Session-aware query? | Semantic hierarchy? | Reads matched body? | Best use |
|---|---:|---:|---:|---|
| `/find` | No | QUICK here; THINKING only with rerank | No | Targeted semantic discovery |
| `/search`, `mode="list"` | Optional | QUICK here; THINKING only with rerank | No hit-body hydration | Session-aware ranked discovery |
| `/search`, `mode="context"` | Optional expansion/dedup | Via scoped `find` calls | Selectively | Heavier server-side assembly (OV budgets/injects); Go SDK does not expose it |
| `/recall` | Optional | Same assembler | Selectively | Deprecated compatibility preset |
| `/grep` | No | No semantic hierarchy | Filesystem exact/regex path; remote backends may use BM25 prefilter | Known phrase or identifier |
| `/content/read` | No | No | Yes | Full selected L2 content |

`mode="context"` does not support `target_uri`, and its `level` field is ignored;
`detail` controls injected content. Supplying `purpose` or `quotas` changes gathering
from flat retrieval to named buckets and makes the bucket quotas, not `limit`, the
candidate ceilings.

## 8. How to Consume `profile.md`

`profile.md` is searchable as an L2 memory record in ordinary list search, but context
assembly explicitly removes it. The reliable contract is a fixed-path read, not
semantic recall.

**Current behavior:** profile has no custom embedding template, so its link-stripped
body is used for both embedding and the indexed `abstract`. Nevertheless,
`gather_candidates()` drops every candidate whose base URI ends in `/profile.md`;
deprecated `/recall` uses the same assembler and therefore also excludes it.

**Recommended consumer contract:** read profile once when building the agent/session
baseline, keep it separate from per-turn relevance search, and refresh it after a
commit that may have updated memory.

```bash
curl --fail --get "$OV_BASE_URL/api/v1/content/read" \
  -H "Authorization: Bearer $OV_API_KEY" \
  --data-urlencode "uri=viking://user/$OV_USER_ID/memories/profile.md"
```

This direct read makes missing-profile handling explicit and prevents a stable
identity baseline from competing with transient memories for per-turn token budget.

## 9. How to Consume Trajectories

Raw trajectories are semantically retrievable L2 evidence, but they are not members
of the named recall quota buckets. Use experiences for routine guidance and targeted
trajectory retrieval for replay, audit, or failure analysis.

Context assembly reports trajectories under catch-all category `memories`. A
quota-free request can retrieve that category, but `purpose="coding"`,
`purpose="chat"`, and deprecated `/recall` activate named buckets that cannot name
`trajectories` or `memories`. Therefore, do not depend on default recall to inject raw
trajectories.

Target raw trajectories with list search:

```bash
curl --fail -X POST "$OV_BASE_URL/api/v1/search/find" \
  -H "Authorization: Bearer $OV_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"verified workflow for repairing an async task hang\",\
       \"target_uri\":\"viking://user/$OV_USER_ID/memories/trajectories\",\
       \"context_type\":\"memory\",\"level\":[2],\"limit\":3}"
```

The match was embedded from `trajectory_name + retrieval_anchor`. Its returned
`abstract` is the stored operation contract; call `/content/read` on the selected URI
when exact fields, provenance links, or full visible content matter.

A quota-free `mode="context"` request with `context_type="memory"` may mix raw
trajectories into a bounded block, but this is opportunistic retrieval, not a
trajectory guarantee. Use the targeted `find -> select -> read` flow when a
trajectory is required.

## 10. Agent Evolution

Agent Evolution is the server-side switch that decides whether session commits may
generate or update `cases`, `trajectories`, and `experiences` — and it gates exactly
those three memory types, nothing else. It is **disabled by default**, so on a fresh
deployment commits produce neither trajectories nor experiences until an admin turns
it on.

### Where the switch lives

- **Instance default:** `server.agent_evolution.enabled` (default `false`),
  `openviking/server/config.py:86-91` (`AgentEvolutionConfig`).
- **Per-account override:** `AccountAgentEvolutionSettings` persisted at
  `/local/{account_id}/_system/setting.json`, resolved by
  `effective_agent_evolution_enabled` — account override wins, else server default
  (`openviking/server/account_settings.py:26-31,109-116`). Hot-reloaded per commit
  via `AgentEvolutionConfigProvider` (`openviking/server/agent_evolution_config.py`).
- **Admin API:** `GET/PUT /api/v1/admin/agent-evolution`
  (`openviking/server/routers/admin.py:91-123`) plus an account-settings PATCH at
  `:388-410` (`:148-149` is the `_check_account_exists` helper, not the endpoint).

Scope is instance-wide default with a per-account override, resolved per account at
session-commit time. There is **no per-session re-enable**:
`memory_policy.memory_types` can further *restrict* but never re-enable the three
types when the switch is off (`openviking/session/session.py:114-130`;
`tests/unit/session/test_agent_evolution_policy.py:30-40` — "disabled agent evolution
cannot be bypassed by session policy").

### What it gates (exactly three types)

The gate set is one constant — `AGENT_EVOLUTION_MEMORY_TYPES = {"cases",
"trajectories", "experiences"}` (`openviking/session/memory/constants.py:4-9`).

- **Disabled (default):** `_apply_agent_evolution_setting` (`session.py:114-130`)
  subtracts those three types from the effective `memory_policy`; skip reason
  `agent_evolution_disabled` is snapshotted into archive metadata
  (`session.py:147-148,1618-1621`). `compressor_v3.py:339-345` subtracts them again in
  long-term extraction, and `compressor_v3.py:377-409` skips
  `train_from_extracted_cases` entirely — so no new cases, trajectories, or
  experiences are written.
- **Enabled:** ordinary cases are produced by the general extraction orchestrator
  inside `_extract_user_memories` (`compressor_v3.py:554-669`, writing via the streaming
  updater at `:596-625`); `_write_training_case_memory` (`compressor_v3.py:480-552`) is
  the separate first-message training-case fast path (called at `:439`).
  `train_from_extracted_cases`
  (`compressor_v3.py:797-1020`) turns each case into trajectories (written by
  `TrajectoryAnalyzer`, `openviking/session/train/components/trajectory_analyzer.py`)
  and experience updates (patch-merged via
  `PatchMergePolicyOptimizer(memory_type="experiences")`,
  `compressor_v3.py:848-870,905-953`). The batch-training fast path requires
  cases+trajectories (`session.py:75`), so it is blocked when the switch is off.

### What it does not touch

`events`, `entities`, `preferences`, `profile`, `identity`, `soul`, and `tools`
are written by the general user-memory extraction path (`compressor_v3.py:554-669`)
regardless of the switch. `resources` are ingested through the resource indexer (not a
memory type that path writes), and session archives (`messages.jsonl`) are persisted
independently during commit Phase 1 (`session.py:2046`, `phase1_persist`) — also
outside the switch. Existing
cases/trajectories/experiences **remain readable and searchable** when the switch is
off — the Agent Evolution product API
(`openviking/service/agent_evolution_service.py`,
`openviking/server/routers/agent_evolution.py`) is read-only lineage over
trajectories/experiences and writes nothing.

### The `skills` caveat

`skills` are co-extracted inside the same commit pipeline under a **separate** flag
(`memory.session_skill_extraction_enabled`), and `extract_session_skills`
(`compressor_v3.py:393-399,677-744`) runs when Agent Evolution is disabled. They are
not *gated* by the switch, but not fully decoupled either: when Agent Evolution is
enabled yet `cases`/`trajectories` are policy-filtered out, the standalone
skill-extraction branch is bypassed (`compressor_v3.py:377-399`).

### Consumer implication

With the switch off (the default), do not expect commits to produce trajectories or
experiences; §9's targeted trajectory retrieval will find only what was written while
the switch was on. The `experiences` and `trajectories` recall buckets are empty by
default until an admin enables Agent Evolution for the account. The VikingBot has no
Agent Evolution administration/toggle surface (zero `agent_evolution` references across
`bot/`), though it does recall cases/experiences and inject an Experience Reminder
(`bot/vikingbot/agent/memory.py:666,714`, `context.py:396`).

## 11. Recommended Consumer Contract

An application should assemble three distinct inputs: stable baseline, relevant
context, and optional execution evidence. This keeps identity, suggestions, and
historical proof from silently replacing one another.

```text
session start
  baseline = read(profile.md)

each turn  (general agents — list mode, light)
  res = search(mode=list, session_id=..., context_type=memory)   # or /find
  hits = res.memories + res.resources + res.skills   # bucketed, not a flat list
  # each hit is one stored node URI; one source document may have several section URIs
  relevant = [h.abstract for h in hits]   # each hit also carries context_type/uri/level/score/tags
  for h in hits where abstract is insufficient: relevant += read(h.uri)

# OPTIONAL: let OV assemble a token-bounded block instead
# recall = search(mode=context, purpose=chat|coding, session_id=...)
# relevant = recall.digest if recall.stats.rewrite == "ok" else recall.rendered

when past execution evidence is required
  hits = find(target_uri=trajectories, level=[2])
  evidence = read(selected_hit.uri)

model input = baseline + relevant + optional evidence
```

For the optional `mode="context"` path, inspect `entries[].uri`, `entries[].detail`,
`stats.tier_counts`, `stats.planned_queries`, `stats.retrieval_errors`, and
`stats.ignored` when debugging. List-mode responses group hits under `memories`/
`resources`/`skills` (each with `context_type`/`uri`/`level`/`score`/`abstract`/`tags`)
plus `total` and optional `query_plan` — they have no `entries[]` or `stats.*`. An empty
result with retrieval errors is not
equivalent to “no relevant memory.” If an entry is served at detail `uri`, explicitly
read it only when the task justifies the extra content and tokens.

## 12. Source Map and Known Documentation Drift

The authoritative implementation is the current code path below. Older conceptual
docs remain useful for storage vocabulary but sometimes describe the retrieval path
too linearly.

- Index levels: [`ContextLevel`](../../../openviking/core/context.py#L34-L39)
- Public hit and result shapes: [`MatchedContext` and `FindResult`](../../../openviking_cli/retrieve/types.py#L283-L390)
- List/context request fields and validation: [`SearchRequest`](../../../openviking/server/routers/search.py#L156-L213)
- `find` and list-mode `search` planning: [`VikingFS`](../../../openviking/storage/viking_fs/_semantic.py#L206-L435)
- Public `find`/`search` mode routing: [`search.py`](../../../openviking/server/routers/search.py#L271-L428)
- Default and no-split document layout: [`MarkdownParser`](../../../openviking/parse/parsers/markdown.py#L1290-L1362)
- Resource parse-mode contract: [`add_resource` API](../../en/api/02-resources.md)
- Intent enable flag: [`RetrievalConfig`](../../../openviking_cli/utils/config/retrieval_config.py#L7-L50)
- Bounded session context for planning: [`Session.get_context_for_search`](../../../openviking/session/session.py#L2989-L3010)
- Intent model selection and typed-query plan: [`IntentAnalyzer`](../../../openviking/retrieve/intent_analyzer.py#L25-L120)
- Resource L0/L1 vector records: [`vectorize_directory_meta`](../../../openviking/utils/embedding_utils.py#L366-L467)
- Memory L2 indexing and embedding templates: [`MemoryUpdater`](../../../openviking/session/memory/memory_updater.py#L1348-L1422)
- QUICK/THINKING retrieval: [`HierarchicalRetriever`](../../../openviking/retrieve/hierarchical_retriever.py#L123-L301)
- Context assembly: [`assemble_context`](../../../openviking/retrieve/context_assembler/pipeline.py#L52-L164)
- Profile filter and flat/bucketed gathering: [`gather_candidates`](../../../openviking/retrieve/context_assembler/gather.py#L227-L389)
- Detail tiers: [`tiers.py`](../../../openviking/retrieve/context_assembler/tiers.py#L106-L226)
- Token budgeting: [`budget.py`](../../../openviking/retrieve/context_assembler/budget.py#L44-L193)
- Deprecated recall overlay: [`recall_preset.py`](../../../openviking/retrieve/context_assembler/recall_preset.py#L41-L115)
- Public context API: [Retrieval API](../../en/api/06-retrieval.md#searchmodecontext)

Do not read existing statements such as “vector search L0, then rerank L1, then load
L2” as literal I/O. This document's no-rerank profile uses QUICK mode and searches
levels together. An optional THINKING deployment would use L0/L1 as directory entry
points and rerank indexed scalars. In both profiles, body reads happen later, only in
context assembly or explicit content access.
