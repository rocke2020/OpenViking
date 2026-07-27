# Path Locks and Crash Recovery

OpenViking combines **path locks**, durable **QueueFS** work, and operation-specific
recovery markers to protect core write operations (`rm`, `mv`, `add_resource`,
`session.commit`) across VikingFS, VectorDB, and background processing.

## Design Philosophy

OpenViking is a context database where FS is the source of truth and VectorDB is a derived index. A lost index can be rebuilt from source data, but lost source data is unrecoverable. Therefore:

> **Better to miss a search result than to return a bad one.**

## Design Principles

1. **Write-exclusive where required**: Protected state transitions use path locks to exclude conflicting writes
2. **Internal by default**: Operations that need transaction protection acquire their own locks; callers do not configure lock scopes
3. **Lock as protection**: LockContext acquires locks on entry, releases on exit — no undo/journal/commit semantics
4. **Operation-specific crash recovery**: QueueFS replays durable background work, while archive and task markers distinguish completed, failed, and interrupted work
5. **Keep slow work outside short critical sections**: A durable queue handoff may occur while a lock is held to close a crash window, but LLM and embedding work runs asynchronously; resource lifecycle locks may deliberately survive that handoff

## Architecture

```
Service Layer (rm / mv / add_resource / session.commit)
    |
    v
+--[LockContext async context manager]--+
|                                       |
|  1. Create LockHandle                 |
|  2. Acquire path lock (poll+timeout)  |
|  3. Execute operations (FS+VectorDB)  |
|  4. Release lock                      |
|                                       |
|  On exception: auto-release lock,     |
|  exception propagates unchanged       |
+---------------------------------------+
    |
    v
Storage Layer (VikingFS, VectorDB, QueueManager)
```

## Two Core Components

### Component 1: PathLockEngine + LockManager + LockContext (Path Lock System)

**PathLockEngine** implements file-based distributed locks with two lock types — EXACT and TREE — using fencing tokens to prevent TOCTOU races and automatic stale lock detection and cleanup.

**LockHandle** is a lightweight lock holder token:

```python
@dataclass
class LockHandle:
    id: str          # Unique ID used to generate fencing tokens
    locks: list[str] # Acquired lock file paths
    created_at: float # Handle creation time
    last_active_at: float # Last successful acquire/refresh time
```

**LockManager** is a global singleton managing lock lifecycle:
- Creates/releases LockHandles
- Background cleanup of leaked locks (in-process safety net)
- Executes legacy RedoLog compatibility recovery on startup

**LockContext** is an async context manager encapsulating the lock/unlock lifecycle:

```python
from openviking.storage.transaction import LockContext, get_lock_manager

async with LockContext(get_lock_manager(), [path], lock_mode="exact") as handle:
    # Perform operations under lock protection
    ...
# Lock automatically released on exit (including exceptions)
```

### Component 2: QueueFS + Archive Markers (Crash Recovery)

Current `session.commit` recovery uses the persistent `SessionCommit` QueueFS queue
plus archive-local state:

- Phase 1 stores its recoverable intent in the archive metadata before rewriting
  the live message root.
- The raw archive is durable before archived messages disappear from the live root.
- Phase 2 writes `.done` last. A replay that sees `.done` skips completed work.
- Terminal failures write `.failed.json`, including the failed stage and any
  completed memory steps.

`LockManager` still scans the legacy RedoLog location on startup when compatibility
recovery is enabled:

```text
/local/_system/redo/{task_id}/redo.json
```

That path recovers markers created by older versions; new session commits rely on
QueueFS and archive markers instead.

## Consistency Issues and Solutions

### rm(uri)

| Problem | Solution |
|---------|----------|
| Delete file first, then index -> file gone but index remains -> search returns non-existent file | **Reverse order**: delete index first, then file. Index deletion failure -> both file and index intact |

**Locking strategy** (depends on target type):
- Deleting a **directory**: `lock_mode="tree"`, locks the directory and its subtree
- Deleting a **file**: `lock_mode="exact"`, locks the file path itself

Operation flow:

```
1. Check whether target is a directory or file, choose lock mode
2. Acquire lock
3. Delete VectorDB index -> immediately invisible to search
4. Delete FS file
5. Release lock
```

VectorDB deletion fails -> exception thrown, lock auto-released, file and index both intact. FS deletion fails -> VectorDB already deleted but file remains, retry is safe.

### mv(old_uri, new_uri)

| Problem | Solution |
|---------|----------|
| File moved to new path but index points to old path -> search returns old path (doesn't exist) | Copy first, then best-effort update index URIs before deleting the source |

**Locking strategy** (handled automatically via `lock_mode="mv"`):
- Moving a **directory**: TreeLock on the source path and ExactPathLock on the destination path
- Moving a **file**: EXACT lock on both source path and destination path

Operation flow:

```
1. Check whether source is a directory or file, set src_is_dir
2. Acquire mv lock (internally chooses TreeLock or ExactPathLock based on src_is_dir)
3. Copy to new location (source still intact, safe)
4. If directory, remove the lock file carried over by cp into the copy
5. Update each VectorDB URI mapping
   - Per-URI failures are logged and processing continues
6. Delete source
7. Release lock
```

**Current limitation:** A copy failure leaves the source intact. However, failures while
updating individual VectorDB URIs do not abort the move, so the source can be deleted while
some index entries still point to the old path. Reindex or otherwise repair the destination
after such a warning.

### add_resource

| Problem | Solution |
|---------|----------|
| File moved from temp to final directory, then crash -> file exists but never searchable | Two separate paths for first-time add vs incremental update |
| Resource already on disk but rm deletes it while semantic processing / vectorization is still running -> wasted work | Lifecycle TreeLock held from finalization through processing completion |

**First-time add** (target does not exist) — handled in `ResourceProcessor.process_resource` Phase 3.5:

```
1. Acquire TreeLock on final_uri
   - If final_uri does not exist, check ancestor/descendant/same-path conflicts first
   - If there is no conflict, create final_uri and write final_uri/.path.ovlock as a T lock
2. Keep temp as the source directory and enqueue SemanticMsg(uri=temp, target_uri=final_uri, lifecycle_lock_handle_id=...)
3. DAG runs on temp and syncs temp content into final_uri after completion
   - Do not use raw agfs.mv(temp -> final_uri), because final_uri already exists for the lock file
4. Clean up temp directory
5. DAG starts lock refresh loop (refreshes the lock token and updates handle activity every lock_expire/2 seconds)
6. DAG complete + all embeddings done -> release TreeLock
```

If summarization and indexing are both disabled, no downstream DAG takes over.
In that case `ResourceProcessor` copies temp directory content into `final_uri`
under the same TreeLock, deletes temp, then releases the lock. It does not call
`VikingFS.mv(temp, final_uri, lock_handle=handle)`, because move cleanup can
remove the directory lock file.

During this period, `rm` attempting to acquire a TreeLock on the same path will fail with `ResourceBusyError`.

**Incremental update** (target already exists) — temp stays in place:

```
1. Acquire TreeLock on target_uri (protect existing resource)
2. Enqueue SemanticMsg(uri=temp, target_uri=final, lifecycle_lock_handle_id=...)
3. DAG runs on temp, lock refresh loop active
4. DAG completion triggers sync_diff_callback or move_temp_to_target_callback
5. Callback completes -> release TreeLock
```

Note: DAG callbacks do NOT wrap operations in an outer lock. Each `VikingFS.rm` and `VikingFS.mv` has its own lock internally. An outer lock would conflict with these inner locks causing deadlock.

Both first-time add and incremental update hold only `TreeLock(resource_dir)`.
There is no `ExactPathLock(resource_dir) -> TreeLock(resource_dir)` handoff, so
the two modes cannot accidentally release the same `.path.ovlock` file in the
wrong scope.

Automatic naming is handled by the resource layer, not the lock service:
`ResourceProcessor` checks `exists(candidate_uri)` first; occupied candidates
try `_1`, `_2`, and so on. Only a non-existing candidate attempts `TreeLock`,
without waiting. If that candidate is busy, the next suffix is tried.

**Server restart recovery**: SemanticMsg is persisted in QueueFS. On restart, `SemanticProcessor` detects that the `lifecycle_lock_handle_id` handle is missing from the in-memory LockManager and re-acquires a TreeLock.

### Derived Semantic Files (.abstract.md / .overview.md)

`.abstract.md` and `.overview.md` are generated sidecar files, not regular user source files. Their concurrency protection has two layers:

| Problem | Solution |
|---------|----------|
| Multiple background tasks refresh the same directory summary and an old result overwrites a newer one | Messages for the same dirty key use `coalesce_version`; only the latest version may write back |
| Two latest-stage writes interleave on the sidecar files | Acquire ExactPathLock on `.abstract.md` and `.overview.md` before writing |

Example: concurrent writes to `docs/a.md`, `docs/b.md`, and `docs/c.md` hold separate ExactPathLocks and do not block each other. Background refresh may start multiple `docs/` summary tasks, but only the latest version writes `docs/.overview.md` and `docs/.abstract.md`; stale tasks drop their results before writeback.

Memory directory summaries use the same rule. Concurrent writes to:

```text
viking://user/default/memories/preferences/theme.md
viking://user/default/memories/preferences/editor.md
```

hold separate ExactPathLocks for the two source files. Refreshing `preferences/.overview.md` and `preferences/.abstract.md` no longer needs a long TreeLock; stale background tasks are filtered by `coalesce_version`, and final sidecar writes briefly acquire ExactPathLock.

### session.commit()

| Problem | Solution |
|---------|----------|
| A stale session instance rewrites the live root while another worker appends messages | Phase 1 reloads and publishes the authoritative message state under the session's ExactPathLock |
| Process exits between archiving messages and starting memory extraction | Phase 1 persists recovery intent, raw messages, and a durable QueueFS item before publishing `phase1.status=ready` |

LLM calls have unpredictable latency (5s~60s+) and must not extend the Phase 1
critical section. The design separates a short, locked state transition from
restart-safe background processing:

```
Phase 1 — Archive handoff (session ExactPathLock):
  1. Reload the authoritative live messages and session metadata
  2. Split messages into archive and retained sets
  3. Persist phase1.status=preparing and the exact recovery intent
  4. Write history/archive_N/messages.jsonl
  5. Enqueue the persistent SessionCommit QueueFS item
  6. Publish retained messages and updated session metadata
  7. Publish phase1.status=ready, then release the lock

Phase 2 — Summary and memory processing (QueueFS worker; LLM work outside the session lock):
  1. Verify phase1.status=ready or reconcile an interrupted Phase 1
  2. Generate the archive summary (LLM)
  3. Extract and write configured memories and relations (LLM)
  4. Enqueue and await required semantic/index work
  5. Update commit metadata
  6. Write .done last; terminal errors write .failed.json
```

**Crash recovery analysis**:

| Failure moment | State | Recovery action |
|------------|-------|----------------|
| Before the QueueFS item is durable | Phase 1 raises and records `.failed.json`; the live root has not been rewritten | Caller may retry without losing the original messages |
| QueueFS item durable, root rewrite not completed | `phase1.status=preparing`; live root still contains archived messages | Worker takes the same session lock, marks the archive failed, and does not process it |
| Root rewrite durable, `phase1.status=ready` not published | Persisted intent and live root prove whether the rewrite completed | Worker reconciles metadata and publishes `ready` before Phase 2 |
| During Phase 2 | QueueFS item is not acknowledged if the worker process exits | QueueFS recovers the item; completed steps and archive markers bound replay |
| Phase 2 complete | `.done` exists | A recovered duplicate skips completed work |
| Phase 2 reaches a terminal error | `.failed.json` exists | The task is terminal and later archives may continue |

## LockContext

`LockContext` is an **async** context manager that encapsulates lock acquisition and release:

```python
from openviking.storage.transaction import LockContext, get_lock_manager

lock_manager = get_lock_manager()

# Exact lock (write operations, semantic processing)
async with LockContext(lock_manager, [path], lock_mode="exact"):
    # Perform operations...
    pass

# Tree lock (directory delete and lifecycle protection)
async with LockContext(lock_manager, [path], lock_mode="tree"):
    # Perform operations...
    pass

# MV lock (move operations)
async with LockContext(lock_manager, [src], lock_mode="mv", mv_dst_path=dst):
    # Perform operations...
    pass
```

**Lock modes**:

| lock_mode | Use case | Behavior |
|-----------|----------|----------|
| `exact` | File writes, single-file delete, sidecar writeback | Lock the specified path; conflicts with same-path locks and ancestor TreeLocks |
| `tree` | Directory delete, resource lifecycle, directory-level protection | Lock the subtree root; conflicts with same-path locks, descendant locks, and ancestor TreeLocks |
| `mv` | Move operations | Directory move: source TreeLock + destination ExactPathLock; File move: ExactPathLock on both source and destination (controlled by `src_is_dir`) |

**Exception handling**: `__aexit__` always releases locks and does not swallow exceptions. Lock acquisition failure raises `LockAcquisitionError`.

## Lock Types (EXACT vs TREE)

The lock mechanism uses two lock types to handle different conflict patterns:

| | EXACT on same path | TREE on same path | EXACT on descendant | TREE on ancestor |
|---|---|---|---|---|
| **EXACT** | Conflict | Conflict | — | Conflict |
| **TREE** | Conflict | Conflict | Conflict | Conflict |

- **EXACT (E)**: Locks one concrete path. It can protect files, directory names, and not-yet-created target paths. Blocks if any ancestor holds a TreeLock.
- **TREE (T)**: Used for directory delete, directory move, resource lifecycle protection, and similar subtree-level operations. Logically covers the entire subtree but only writes **one lock file** at the root. Before acquiring, scans all descendants and ancestor directories for conflicting locks. If the target directory is missing, conflicts are checked first; only then is the directory created and locked. If a later double-check finds a new conflict, the acquire fails or retries without rolling back the empty directory.

## Lock Mechanism

### Lock Protocol

Lock file paths:

```text
TreeLock(path)                  -> {path}/.path.ovlock
ExactPathLock(existing dir path) -> {path}/.path.ovlock
ExactPathLock(file or missing path) -> {parent}/.exact.ovlock.<name>.<hash>
```

Lock file content (Fencing Token):
```
{handle_id}:{time_ns}:{lock_type}
```

Where `lock_type` is `E` (EXACT) or `T` (TREE).

### Lock Acquisition (EXACT mode)

```
loop until timeout (poll interval: 100ms):
    1. Check if target path is locked by another operation
       - Stale lock? -> remove and retry
       - Active lock? -> wait
    2. Check all ancestor directories for TREE locks
       - Stale lock? -> remove and retry
       - Active lock? -> wait
    3. Ensure the lock file's parent directory exists; create it if missing
    4. Write EXACT (E) lock file
    5. TOCTOU double-check: re-scan target path and ancestors for TREE locks
       - Conflict found: compare (timestamp, handle_id)
       - Later one (larger timestamp/handle_id) backs off (removes own lock) to prevent livelock
       - Wait and retry
    6. Verify lock file ownership (fencing token matches)
    7. Success

Timeout (default 0 = no-wait) raises LockAcquisitionError
```

### Lock Acquisition (TREE mode)

```
loop until timeout (poll interval: 100ms):
    1. Check if target directory is locked by another operation
       - Stale lock? -> remove and retry
       - Active lock? -> wait
    2. Check all ancestor directories for TREE locks
       - Stale lock? -> remove and retry
       - Active lock? -> wait
    3. Scan all descendant directories for any locks by other operations
       - Missing target directory? -> treat as no descendant locks
       - Stale lock? -> remove and retry
       - Active lock? -> wait
    4. Ensure the target directory exists; create it if missing
    5. Write TREE (T) lock file (only one file, at the root path)
    6. TOCTOU double-check: re-scan descendants and ancestors
       - Conflict found: compare (timestamp, handle_id)
       - Later one (larger timestamp/handle_id) backs off (removes own lock) to prevent livelock
       - Wait and retry
    7. Verify lock file ownership (fencing token matches)
    8. Success

Timeout (default 0 = no-wait) raises LockAcquisitionError
```

### Missing Directory Creation

The lock system may create directories so it can place lock files, but it checks
for conflicts first:

```
1. Ancestor TreeLock / same-path lock / descendant lock conflict -> do not create the directory
2. No current conflict -> create the directory and write the lock
3. A post-write double-check finds a new conflict -> remove our own lock and fail or retry
4. Step 3 does not roll back the empty directory
```

### Lock Expiry Cleanup

**Stale lock detection**: PathLockEngine checks the fencing token timestamp. Locks older than `lock_expire` (default 1800s / 30 minutes) are considered stale and are removed automatically during acquisition.

**In-process cleanup**: LockManager checks active LockHandles every 60 seconds. Handles that still own lock files but have been inactive for longer than `lock_expire` are force-released.

**Orphan locks**: Lock files left behind after a process crash are automatically removed via stale lock detection when any operation next attempts to acquire a lock on the same path.

## Crash Recovery

QueueFS workers resume durable work on startup. `LockManager.start()` also scans
`/local/_system/redo/` for legacy compatibility markers when
`redo_recovery_enabled` is true:

| Scenario | Recovery action |
|----------|----------------|
| Current `session.commit` Phase 2 worker exits | QueueFS re-delivers the unacknowledged `SessionCommit` item; archive markers make replay restart-safe |
| Legacy session-memory redo marker remains | LockManager replays the legacy marker when compatibility recovery is enabled |
| Crash while holding lock | Lock file remains in AGFS; stale detection auto-cleans on next acquisition (default 1800s / 30-minute expiry) |
| Crash after enqueue, before worker processes | QueueFS SQLite persistence; worker auto-pulls after restart |
| Orphan index | Cleaned on L2 on-demand load |

### Defense Summary

| Failure scenario | Defense | Recovery timing |
|-----------------|--------|-----------------|
| Crash during operation | Lock auto-expires + stale detection | Next acquisition of same path lock |
| Crash during add_resource semantic processing | Lifecycle lock expires + SemanticProcessor re-acquires on restart | Worker restart |
| Crash during session.commit Phase 1 | Recoverable intent + authoritative live root reconciliation under the session lock | QueueFS worker |
| Crash during session.commit Phase 2 | Persistent SessionCommit item + archive `.done` / `.failed.json` markers | QueueFS recovery after restart |
| Crash after enqueue, before worker | QueueFS SQLite persistence | Worker restart |
| Orphan index | L2 on-demand load cleanup | When user accesses |

## Configuration

Path locks are enabled by default with no extra configuration needed. **The default behavior is no-wait**: if the path is locked, `LockAcquisitionError` is raised immediately. To allow wait/retry, configure the `storage.transaction` section:

```json
{
  "storage": {
    "transaction": {
      "lock_timeout": 5.0,
      "lock_expire": 1800.0,
      "redo_recovery_enabled": true
    }
  }
}
```

| Parameter | Type | Description | Default |
|-----------|------|-------------|---------|
| `lock_timeout` | float | Lock acquisition timeout (seconds). `0` = fail immediately if locked (default). `> 0` = wait/retry up to this many seconds. | `0.0` |
| `lock_expire` | float | Lock inactivity threshold (seconds). Locks not refreshed within this window are treated as stale and reclaimed. | `1800.0` |
| `redo_recovery_enabled` | bool | Enable startup recovery of legacy RedoLog markers. Current session commits use QueueFS recovery independently of this setting. | `true` |

### QueueFS Persistence

Durable background recovery relies on QueueFS using the SQLite backend so enqueued
tasks survive process restarts. This is the default configuration and requires no
manual setup.

## Related Documentation

- [Architecture](./01-architecture.md) - System architecture overview
- [Storage](./05-storage.md) - AGFS and vector store
- [Session Management](./08-session.md) - Session and memory management
- [Configuration](../guides/01-configuration.md) - Configuration reference
