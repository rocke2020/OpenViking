# How OpenViking Data Is Used During Search and Recall

> One-sentence conclusion: use L0/L1 as indexed navigation, use L2 only when the
> answer needs leaf content, load `profile.md` explicitly as stable baseline context,
> and retrieve raw trajectories deliberately rather than expecting default recall to
> include them.

## TL;DR

OpenViking does not execute a fixed “read L0, then L1, then L2” pipeline. It searches
independent vector records, may use directory L0/L1 records to reach L2 leaves, and
only reads stored bodies when context assembly or the caller asks for more detail.

- **At session start:** read `viking://user/<user_id>/memories/profile.md` once and
  inject it as stable user context. Context recall intentionally filters this file.
- **On each turn:** use `POST /api/v1/search/search` with `mode="context"`; inject its
  `digest` when rewrite succeeds, otherwise its `rendered` block.
- **For future action guidance:** prefer `experiences`, which has a normal recall quota.
- **For historical execution evidence:** target
  `viking://user/<user_id>/memories/trajectories` with `find`, select an L2 hit, and
  `read` it when the indexed result is not sufficient.
- **Do not combine the level systems:** list search uses numeric index levels
  `0/1/2`; context assembly uses content detail tiers
  `uri/abstract/overview/full`.

## Scope and Verification Boundary

This document describes source-confirmed behavior at commit
`9dc9b18aaf18bc23df03775894c896eb0bd700ae`. Runtime features such as sparse vectors,
reranking, memory extraction, and Agent Evolution remain configuration-dependent.

The code and focused test assertions were reviewed; no live server, model, vector
database, or user data was exercised for this document. Sections labelled
**Recommended consumer contract** describe integration guidance, not an automatic OV
runtime guarantee.

## 1. What Commit Produces

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

## 2. The Two Level Systems

The storage/index level identifies what node was matched; the assembly detail tier
identifies how much text will be injected. They overlap in vocabulary but are not a
one-to-one mapping.

| System | Value | Meaning | Does it require a content read? |
|---|---|---|---|
| Index | L0 / `level=0` | Directory `.abstract.md` vector record | No; text is in the vector payload |
| Index | L1 / `level=1` | Directory `.overview.md` vector record | No; overview is copied into the payload's `abstract` scalar |
| Index | L2 / `level=2` | Leaf file, including memory Markdown | List search does not read the hit body |
| Assembly | `uri` | Pointer only | No |
| Assembly | `abstract` | Candidate `abstract` scalar | No |
| Assembly | `overview` | Directory sidecar or a summary/skeleton derived from a leaf | Yes |
| Assembly | `full` | Full visible leaf content | Yes |

Two consequences matter:

1. An L2 memory served at assembly detail `abstract` may already expose its complete
   memory body because memory indexing stores that body in the scalar.
2. An L2 memory served at detail `overview` is usually read and compressed from that
   same leaf file; it is not necessarily a directory L1 `.overview.md` record.

For resources, an L2 embedding can use full text, a generated summary, a filename, or
multimodal input depending on content type and embedding configuration. Therefore,
“L2 vector equals the complete original file” is not a valid general assumption.

## 3. How List Search Uses L0, L1, and L2

List search returns ranked index records, not hydrated bodies. Its use of the three
levels changes according to whether a reranker is available.

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
specified. `level=[2]` is pushed directly into the vector query.

In THINKING mode, global search first selects L0/L1 directory entry points. It then
searches direct children and propagates scores; only non-L2 results continue the
recursion. Reranking uses each index record's `abstract` scalar and does not read the
corresponding AGFS file. A `level=[2]` filter limits returned candidates but may still
use L0/L1 internally as traversal routes.

The returned list item contains `uri`, `level`, `score`, indexed `abstract`, and tags.
Display URIs for L0/L1 reconstruct the `/.abstract.md` or `/.overview.md` suffix. To
obtain a selected L2 file's visible content, call `read(hit.uri)` explicitly.

## 4. How Context Search Uses the Retrieved Data

`mode="context"` turns ranked hits into an injection-ready, token-bounded block. It
uses the same semantic index underneath but performs selective reads only after
candidate retrieval and ranking.

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

## 5. API Choice

Choose the API by the result the agent needs: discovery, ready-to-inject context, an
exact text match, or complete content.

| API | Session-aware query? | Semantic hierarchy? | Reads matched body? | Best use |
|---|---:|---:|---:|---|
| `/find` | No | QUICK or THINKING | No | Targeted semantic discovery |
| `/search`, `mode="list"` | Optional | QUICK or THINKING | No hit-body hydration | Session-aware ranked discovery |
| `/search`, `mode="context"` | Optional expansion/dedup | Via scoped `find` calls | Selectively | Primary LLM context assembly |
| `/recall` | Optional | Same assembler | Selectively | Deprecated compatibility preset |
| `/grep` | No | No semantic hierarchy | Filesystem exact/regex path; remote backends may use BM25 prefilter | Known phrase or identifier |
| `/content/read` | No | No | Yes | Full selected L2 content |

`mode="context"` does not support `target_uri`, and its `level` field is ignored;
`detail` controls injected content. Supplying `purpose` or `quotas` changes gathering
from flat retrieval to named buckets and makes the bucket quotas, not `limit`, the
candidate ceilings.

## 6. How to Consume `profile.md`

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

## 7. How to Consume Trajectories

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

## 8. Recommended Consumer Contract

An application should assemble three distinct inputs: stable baseline, relevant
context, and optional execution evidence. This keeps identity, suggestions, and
historical proof from silently replacing one another.

```text
session start
  baseline = read(profile.md)

each turn
  recall = search(mode=context, purpose=chat|coding, session_id=...)
  if recall.stats.rewrite == "ok" and recall.digest:
      relevant = recall.digest
  elif recall.stats.rewrite == "no_relevant":
      relevant = ""
  else:
      relevant = recall.rendered

when past execution evidence is required
  hits = find(target_uri=trajectories, level=[2])
  evidence = read(selected_hit.uri)

model input = baseline + relevant + optional evidence
```

Inspect `entries[].uri`, `entries[].detail`, `stats.tier_counts`,
`stats.planned_queries`, `stats.retrieval_errors`, and `stats.ignored` when debugging.
An empty result with retrieval errors is not equivalent to “no relevant memory.” If
an entry is served at detail `uri`, explicitly read it only when the task justifies
the extra content and tokens.

## 9. Source Map and Known Documentation Drift

The authoritative implementation is the current code path below. Older conceptual
docs remain useful for storage vocabulary but sometimes describe the retrieval path
too linearly.

- Index levels: [`ContextLevel`](../../../openviking/core/context.py#L34-L38)
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
L2” as literal I/O. QUICK mode searches levels together; THINKING mode uses L0/L1 as
directory entry points; reranking uses indexed scalars; and body reads happen later,
only in context assembly or explicit content access.
