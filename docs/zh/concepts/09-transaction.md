# 路径锁与崩溃恢复

OpenViking 组合使用**路径锁**、持久化 **QueueFS** 任务和操作专属的恢复标记，
保护 VikingFS、VectorDB 与后台处理中的核心写操作（`rm`、`mv`、`add_resource`、
`session.commit`）。

## 设计哲学

OpenViking 是上下文数据库，FS 是源数据，VectorDB 是派生索引。索引丢了可从源数据重建，源数据丢失不可恢复。因此：

> **宁可搜不到，不要搜到坏结果。**

## 设计原则

1. **在需要的位置保证写互斥**：受保护的状态转换使用路径锁排除冲突写入
2. **默认由内部处理**：需要事务保护的操作自行获取适当范围的锁，调用方无需配置锁范围
3. **锁即保护**：进入 LockContext 时加锁，退出时释放，没有 undo/journal/commit 语义
4. **按操作设计崩溃恢复**：QueueFS 重放持久化后台任务，archive 和任务标记区分完成、失败与中断状态
5. **慢任务不占用短临界区**：为关闭崩溃窗口，持锁期间可以完成持久化队列交接；LLM 和 embedding 工作异步执行，资源生命周期锁则可能有意跨越这一交接继续持有

## 架构

```
Service Layer (rm / mv / add_resource / session.commit)
    |
    v
+--[LockContext 异步上下文管理器]-------+
|                                       |
|  1. 创建 LockHandle                  |
|  2. 获取路径锁（轮询 + 超时）        |
|  3. 执行操作（FS + VectorDB）        |
|  4. 释放锁                           |
|                                       |
|  异常时：自动释放锁，异常原样传播    |
+---------------------------------------+
    |
    v
Storage Layer (VikingFS, VectorDB, QueueManager)
```

## 两个核心组件

### 组件 1：PathLockEngine + LockManager + LockContext（路径锁系统）

**PathLockEngine** 实现基于文件的分布式锁，支持 EXACT 和 TREE 两种锁类型，使用 fencing token 防止 TOCTOU 竞争，自动检测并清理过期锁。

**LockHandle** 是轻量的锁持有者令牌：

```python
@dataclass
class LockHandle:
    id: str          # 唯一标识，用于生成 fencing token
    locks: list[str] # 已获取的锁文件路径
    created_at: float # handle 创建时间
    last_active_at: float # 最近一次成功 acquire/refresh 的时间
```

**LockManager** 是全局单例，管理锁生命周期：
- 创建/释放 LockHandle
- 后台清理泄漏的锁（进程内安全网）
- 启动时执行旧版 RedoLog 的兼容恢复

**LockContext** 是异步上下文管理器，封装加锁/解锁生命周期：

```python
from openviking.storage.transaction import LockContext, get_lock_manager

async with LockContext(get_lock_manager(), [path], lock_mode="exact") as handle:
    # 在锁保护下执行操作
    ...
# 退出时自动释放锁（包括异常情况）
```

### 组件 2：QueueFS + Archive 标记（崩溃恢复）

当前 `session.commit` 使用持久化的 `SessionCommit` QueueFS 队列和 archive
本地状态完成恢复：

- Phase 1 在改写实时消息根之前，把可恢复意图写入 archive 元数据。
- 归档消息从实时消息根消失之前，原始 archive 已经持久化。
- Phase 2 最后写入 `.done`；重放看到 `.done` 时跳过已完成任务。
- 终态失败写入 `.failed.json`，其中包含失败阶段和已经完成的 memory 步骤。

启用兼容恢复时，`LockManager` 启动后仍会扫描旧版 RedoLog 路径：

```text
/local/_system/redo/{task_id}/redo.json
```

该路径只用于恢复旧版本创建的标记；新的 session commit 依赖 QueueFS 和
archive 标记。

## 一致性问题与解决方案

### rm(uri)

| 问题 | 方案 |
|------|------|
| 先删文件再删索引 -> 文件已删但索引残留 -> 搜索返回不存在的文件 | **调换顺序**：先删索引再删文件。索引删除失败 -> 文件和索引都在，搜索正常 |

**加锁策略**（根据目标类型区分）：
- 删除**目录**：`lock_mode="tree"`，锁目录自身及其整棵子树
- 删除**文件**：`lock_mode="exact"`，锁文件路径本身

操作流程：

```
1. 检查目标是目录还是文件，选择锁模式
2. 获取锁
3. 删除 VectorDB 索引 -> 搜索立刻不可见
4. 删除 FS 文件
5. 释放锁
```

VectorDB 删除失败 -> 直接抛异常，锁自动释放，文件和索引都在。FS 删除失败 -> VectorDB 已删但文件还在，重试即可。

### mv(old_uri, new_uri)

| 问题 | 方案 |
|------|------|
| 文件移到新路径但索引指向旧路径 -> 搜索返回旧路径（不存在） | 先 copy，再逐项尽力更新索引 URI，然后删除源路径 |

**加锁策略**（通过 `lock_mode="mv"` 自动处理）：
- 移动**目录**：源路径加 TreeLock，目标路径加 ExactPathLock
- 移动**文件**：源路径和目标路径各加 EXACT 锁

操作流程：

```
1. 检查源是目录还是文件，确定 src_is_dir
2. 获取 mv 锁（内部根据 src_is_dir 选择 TreeLock 或 ExactPathLock）
3. Copy 到新位置（源还在，安全）
4. 如果是目录，删除副本中被 cp 带过去的锁文件
5. 逐项更新 VectorDB 中的 URI 映射
   - 单个 URI 更新失败时记录 warning，并继续处理
6. 删除源
7. 释放锁
```

**当前限制：** copy 失败时源路径仍然保留；但单个 VectorDB URI 更新失败不会中止
移动，因此源路径可能已经删除，而部分索引仍指向旧路径。看到这类 warning 后，需要
对目标路径重新索引或进行其他修复。

### add_resource

| 问题 | 方案 |
|------|------|
| 文件从临时目录移到正式目录后崩溃 -> 文件存在但永远搜不到 | 首次添加与增量更新分离为两条独立路径 |
| 资源已落盘但语义处理/向量化还在跑时被 rm 删除 -> 处理白跑 | 生命周期 TreeLock，从落盘持续到处理完成 |

**首次添加**（target 不存在）— 在 `ResourceProcessor.process_resource` Phase 3.5 中处理：

```
1. 获取 TreeLock，锁 final_uri
   - 如果 final_uri 目录不存在，先检查祖先/后代/同路径锁冲突
   - 无冲突则创建 final_uri 目录，并在 final_uri/.path.ovlock 写 T 锁
2. 保留 temp 作为源目录，入队 SemanticMsg(uri=temp, target_uri=final_uri, lifecycle_lock_handle_id=...)
3. DAG 在 temp 上跑，完成后把 temp 内容同步到 final_uri
   - final_uri 已经用于放锁文件，所以不做裸 agfs.mv(temp -> final_uri)
4. 清理临时目录
5. DAG 启动锁刷新循环（每 lock_expire/2 秒刷新锁 token 并更新 handle 活跃时间）
6. DAG 完成 + 所有 embedding 完成 -> 释放 TreeLock
```

如果本次调用关闭了摘要和索引（没有下游 DAG 接管），则在同一把 TreeLock
里把 temp 目录内容复制到 `final_uri`，清理 temp，然后释放锁。这里不调用
`VikingFS.mv(temp, final_uri, lock_handle=handle)`，避免移动逻辑清理目录锁文件。

此期间 `rm` 尝试获取同路径 TreeLock 会失败，抛出 `ResourceBusyError`。

**增量更新**（target 已存在）— temp 保持不动：

```
1. 获取 TreeLock，锁 target_uri（保护已有资源）
2. 入队 SemanticMsg(uri=temp, target_uri=final, lifecycle_lock_handle_id=...)
3. DAG 在 temp 上跑，启动锁刷新循环
4. DAG 完成后触发 sync_diff_callback 或 move_temp_to_target_callback
5. callback 执行完毕 -> 释放 TreeLock
```

注意：DAG callback 不在外层加锁。每个 `VikingFS.rm` 和 `VikingFS.mv` 内部各自有独立锁保护。外层锁会与内部锁冲突导致死锁。

首次添加和增量更新都只持有 `TreeLock(resource_dir)`。这里不再做
`ExactPathLock(resource_dir) -> TreeLock(resource_dir)` 的锁转交，避免两种锁复用
同一个 `.path.ovlock` 时出现释放顺序错误。

自动命名由资源层处理，不属于锁服务：`ResourceProcessor` 先用 `exists(candidate_uri)`
判断候选目录是否已占用；已存在则尝试 `_1`、`_2` 后缀。候选目录不存在时才尝试
获取该目录的 `TreeLock`，且不等待；如果同名正在被并发请求处理，就直接尝试下一个后缀。

**服务重启恢复**：SemanticMsg 持久化在 QueueFS 中。重启后 `SemanticProcessor` 发现 `lifecycle_lock_handle_id` 对应的 handle 不在内存中，会重新获取 TreeLock。

### 派生语义文件（.abstract.md / .overview.md）

`.abstract.md` 和 `.overview.md` 是后台生成的派生文件，不作为普通用户源文件写入。它们的并发保护分两层：

| 问题 | 方案 |
|------|------|
| 多个后台任务同时刷新同一个目录摘要，旧结果覆盖新结果 | 相同 dirty key 使用 `coalesce_version`，只有最新版本允许写回 |
| 最新任务写回派生文件时与另一个写回交错 | 写 `.abstract.md`、`.overview.md` 前获取各自的 ExactPathLock |

例子：同一目录下并发写入 `a.md`、`b.md`、`c.md` 时，前台写入分别持有 `ExactPathLock(a.md)`、`ExactPathLock(b.md)`、`ExactPathLock(c.md)`，互不阻塞。后台可能产生多个 `docs/` 摘要刷新任务，但只有最新 version 能写回 `docs/.overview.md` 和 `docs/.abstract.md`；旧任务在写回前发现自己过期后直接丢弃结果。

memory 目录摘要使用同一规则。比如并发更新：

```text
viking://user/default/memories/preferences/theme.md
viking://user/default/memories/preferences/editor.md
```

两个文件写入各自持有 ExactPathLock；`preferences/.overview.md` 和 `preferences/.abstract.md` 的后台刷新不再持有长时间 TreeLock，而是通过 `coalesce_version` 淘汰旧任务，并在最终写派生文件时短暂获取 ExactPathLock。

### session.commit()

| 问题 | 方案 |
|------|------|
| 过期 session 实例改写实时消息根，同时另一个 worker 正在追加消息 | Phase 1 在 session ExactPathLock 下重新加载并发布权威消息状态 |
| 进程在归档消息后、开始 memory 提取前退出 | Phase 1 先持久化恢复意图、原始消息和 QueueFS 任务，再发布 `phase1.status=ready` |

LLM 调用耗时不可控（5s~60s+），不能延长 Phase 1 的临界区。设计把短时间的
加锁状态转换与可重启的后台处理拆开：

```
Phase 1 — Archive 交接（session ExactPathLock）：
  1. 重新加载权威的实时消息和 session 元数据
  2. 把消息拆分为归档集合和保留集合
  3. 持久化 phase1.status=preparing 和完整恢复意图
  4. 写入 history/archive_N/messages.jsonl
  5. 把持久化 SessionCommit 任务加入 QueueFS
  6. 发布保留消息和更新后的 session 元数据
  7. 发布 phase1.status=ready，然后释放锁

Phase 2 — 摘要和 memory 处理（QueueFS worker；LLM 工作在 session 锁外）：
  1. 验证 phase1.status=ready，或协调被中断的 Phase 1
  2. 生成 archive 摘要（LLM）
  3. 提取并写入配置的 memories 和 relations（LLM）
  4. 入队并等待必要的语义/索引任务
  5. 更新 commit 元数据
  6. 最后写入 .done；终态错误写入 .failed.json
```

**崩溃恢复分析**：

| 崩溃时间点 | 状态 | 恢复动作 |
|-----------|------|---------|
| QueueFS 任务持久化之前 | Phase 1 抛出异常并记录 `.failed.json`；实时消息根尚未改写 | 调用方可以重试，原始消息不会丢失 |
| QueueFS 任务已持久化，但实时消息根尚未改写 | `phase1.status=preparing`；实时消息根仍包含被归档消息 | Worker 获取同一把 session 锁，把 archive 标记为失败且不处理该任务 |
| 实时消息根已改写，但尚未发布 `phase1.status=ready` | 持久化意图和实时消息根可以证明改写是否完成 | Worker 协调元数据并发布 `ready`，然后进入 Phase 2 |
| Phase 2 期间 | Worker 进程退出时 QueueFS 任务不会被 ACK | QueueFS 恢复任务；已完成步骤和 archive 标记约束重放范围 |
| Phase 2 完成 | `.done` 已存在 | 恢复出的重复任务跳过已完成工作 |
| Phase 2 发生终态错误 | `.failed.json` 已存在 | 该任务进入终态，后续 archive 仍可继续 |

## LockContext

`LockContext` 是**异步**上下文管理器，封装锁的获取和释放：

```python
from openviking.storage.transaction import LockContext, get_lock_manager

lock_manager = get_lock_manager()

# Exact 锁（写操作、语义处理）
async with LockContext(lock_manager, [path], lock_mode="exact"):
    # 执行操作...
    pass

# Tree 锁（删除目录、目录生命周期保护）
async with LockContext(lock_manager, [path], lock_mode="tree"):
    # 执行操作...
    pass

# MV 锁（移动操作）
async with LockContext(lock_manager, [src], lock_mode="mv", mv_dst_path=dst):
    # 执行操作...
    pass
```

**锁模式**：

| lock_mode | 用途 | 行为 |
|-----------|------|------|
| `exact` | 文件写入、单文件删除、派生文件写回 | 锁定指定路径；与同路径锁和祖先目录 TreeLock 冲突 |
| `tree` | 删除目录、资源生命周期、目录级保护 | 锁定子树根节点；与同路径锁、后代锁和祖先 TreeLock 冲突 |
| `mv` | 移动操作 | 目录移动：源路径 TreeLock + 目标路径 ExactPathLock；文件移动：源路径和目标路径均 ExactPathLock（通过 `src_is_dir` 控制） |

**异常处理**：`__aexit__` 总是释放锁，不吞异常。获取锁失败时抛出 `LockAcquisitionError`。

## 锁类型（EXACT vs TREE）

锁机制使用两种锁类型来处理不同的冲突场景：

| | 同路径 EXACT | 同路径 TREE | 后代 EXACT | 祖先 TREE |
|---|---|---|---|---|
| **EXACT** | 冲突 | 冲突 | — | 冲突 |
| **TREE** | 冲突 | 冲突 | 冲突 | 冲突 |

- **EXACT (E)**：锁定一个具体路径本身。文件、目录名、尚未创建的目标路径都可以使用；若祖先目录持有 TreeLock 则阻塞。
- **TREE (T)**：用于删除目录、移动目录、资源生命周期保护等。逻辑上覆盖整棵子树，但只在根目录写**一个锁文件**。获取前扫描所有后代和祖先目录确认无冲突锁。目标目录不存在时，先做冲突检查；无冲突才创建目录并写锁。若创建后又发现并发冲突，本次加锁失败，但不回滚刚创建出来的空目录。

## 锁机制

### 锁协议

锁文件路径：

```text
TreeLock(path)                 -> {path}/.path.ovlock
ExactPathLock(已存在目录 path) -> {path}/.path.ovlock
ExactPathLock(文件或未创建路径) -> {parent}/.exact.ovlock.<name>.<hash>
```

锁文件内容（Fencing Token）：
```
{handle_id}:{time_ns}:{lock_type}
```

其中 `lock_type` 为 `E`（EXACT）或 `T`（TREE）。

### 获取锁流程（EXACT 模式）

```
循环直到超时（轮询间隔：100ms）：
    1. 检查目标路径是否被其他操作锁定
       - 陈旧锁？ -> 移除后重试
       - 活跃锁？ -> 等待
    2. 检查所有祖先目录是否有 TREE 锁
       - 陈旧锁？ -> 移除后重试
       - 活跃锁？ -> 等待
    3. 确保锁文件所在父目录存在；如果不存在则创建目录
    4. 写入 EXACT (E) 锁文件
    5. TOCTOU 双重检查：重新扫描目标路径和祖先目录的 TREE 锁
       - 发现冲突：比较 (timestamp, handle_id)
       - 后到者（更大的 timestamp/handle_id）主动让步（删除自己的锁），防止活锁
       - 等待后重试
    6. 验证锁文件归属（fencing token 匹配）
    7. 成功

超时（默认 0 = 不等待）抛出 LockAcquisitionError
```

### 获取锁流程（TREE 模式）

```
循环直到超时（轮询间隔：100ms）：
    1. 检查目标路径是否被其他操作锁定
       - 陈旧锁？ -> 移除后重试
       - 活跃锁？ -> 等待
    2. 检查所有祖先目录是否有 TREE 锁
       - 陈旧锁？ -> 移除后重试
       - 活跃锁？ -> 等待
    3. 扫描所有后代目录，检查是否有其他操作持有的锁
       - 目标目录不存在？ -> 视为无后代锁
       - 陈旧锁？ -> 移除后重试
       - 活跃锁？ -> 等待
    4. 确保目标目录存在；如果不存在则创建目录
    5. 写入 TREE (T) 锁文件（只写一个文件，在根路径）
    6. TOCTOU 双重检查：重新扫描后代目录和祖先目录
       - 发现冲突：比较 (timestamp, handle_id)
       - 后到者（更大的 timestamp/handle_id）主动让步（删除自己的锁），防止活锁
       - 等待后重试
    7. 验证锁文件归属（fencing token 匹配）
    8. 成功

超时（默认 0 = 不等待）抛出 LockAcquisitionError
```

### 缺失目录创建规则

锁系统允许为了放置锁文件而创建目录，但创建前必须先检查冲突：

```
1. 发现祖先 TreeLock / 同路径锁 / 后代锁冲突 -> 不创建目录，直接失败或等待
2. 当前无冲突 -> 可以创建目录并写锁
3. 写锁后再次检查时发现新冲突 -> 删除自己的锁并失败或重试
4. 第 3 步不会回滚刚创建的空目录
```

例子：

```text
请求 A 正在删除 viking://resources/books
=> A 持有 TreeLock(/resources/books)

请求 B 想添加 viking://resources/books/java-guide
=> B 在创建 java-guide 目录前发现祖先 TreeLock
=> B 不创建目录，返回 busy
```

如果两个请求同时创建 `java-guide`，两边都可能先看到“当前无冲突”，但最终只有
fencing token 校验通过的一方成功持有 `TreeLock(java-guide)`；失败方会删除自己的锁，
已创建出来的空目录可以保留。

### 锁过期清理

**陈旧锁检测**：PathLockEngine 检查 fencing token 中的时间戳。超过 `lock_expire`（默认 1800s / 30 分钟）的锁被视为陈旧锁，在加锁过程中自动移除。

**进程内清理**：LockManager 每 60 秒检查活跃的 LockHandle。仍持有锁文件且失活时间超过 `lock_expire` 的 handle 会被强制释放。

**孤儿锁**：进程崩溃后遗留的锁文件，在下次任何操作尝试获取同一路径锁时，通过 stale lock 检测自动移除。

## 崩溃恢复

QueueFS worker 启动后恢复持久化任务。启用 `redo_recovery_enabled` 时，
`LockManager.start()` 还会扫描 `/local/_system/redo/` 中的旧版兼容标记：

| 场景 | 恢复方式 |
|------|---------|
| 当前 `session.commit` Phase 2 worker 退出 | QueueFS 重新投递未 ACK 的 `SessionCommit` 任务；archive 标记保证重放可安全恢复 |
| 遗留旧版 session-memory redo 标记 | 启用兼容恢复时，LockManager 重放旧版标记 |
| 锁持有期间崩溃 | 锁文件留在 AGFS，下次获取时 stale 检测自动清理（默认 1800s / 30 分钟过期）|
| enqueue 后 worker 处理前崩溃 | QueueFS SQLite 持久化，worker 重启后自动拉取 |
| 孤儿索引 | L2 按需加载时清理 |

### 防线总结

| 异常场景 | 防线 | 恢复时机 |
|---------|------|---------|
| 操作中途崩溃 | 锁自动过期 + stale 检测 | 下次获取同路径锁时 |
| add_resource 语义处理中途崩溃 | 生命周期锁过期 + SemanticProcessor 重启时重新获取 | worker 重启后 |
| session.commit Phase 1 崩溃 | 可恢复意图 + 在 session 锁下协调权威实时消息根 | QueueFS worker |
| session.commit Phase 2 崩溃 | 持久化 SessionCommit 任务 + archive `.done` / `.failed.json` 标记 | QueueFS 恢复 |
| enqueue 后 worker 处理前崩溃 | QueueFS SQLite 持久化 | worker 重启后 |
| 孤儿索引 | L2 按需加载时清理 | 用户访问时 |

## 配置

路径锁默认启用，无需额外配置。**默认不等待**：若路径被锁定则立即抛出 `LockAcquisitionError`。如需允许等待重试，可通过 `storage.transaction` 段配置：

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

| 参数 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| `lock_timeout` | float | 获取锁的等待超时（秒）。`0` = 立即失败（默认）；`> 0` = 最多等待此时间 | `0.0` |
| `lock_expire` | float | 锁失活阈值（秒），超过此时间未被 refresh 的锁会被视为陈旧锁并回收 | `1800.0` |
| `redo_recovery_enabled` | bool | 启用旧版 RedoLog 标记的启动恢复。当前 session commit 使用独立的 QueueFS 恢复，不受该配置影响 | `true` |

### QueueFS 持久化

持久化后台恢复依赖 QueueFS 使用 SQLite 后端，使已入队任务可以跨进程重启存活。
这是默认配置，无需手动设置。

## 相关文档

- [架构概述](./01-architecture.md) - 系统整体架构
- [存储架构](./05-storage.md) - AGFS 和向量库
- [会话管理](./08-session.md) - 会话和记忆管理
- [配置](../guides/01-configuration.md) - 配置文件说明
