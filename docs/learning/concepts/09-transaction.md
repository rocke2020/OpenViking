# OpenViking 路径锁与崩溃恢复学习笔记

> 原文：[Path Locks and Crash Recovery](../../en/concepts/09-transaction.md)

## TL;DR

OpenViking 的“事务”不是数据库式 ACID 事务，而是三种机制的组合：
**路径锁阻止重叠写入，操作顺序把不可恢复的数据放在更安全的位置，
QueueFS 和操作专属标记恢复特定的异步工作**。旧版 RedoLog 只承担兼容恢复。
理解这三层的边界，比把
`LockContext` 想成一个能够自动回滚的 transaction 更准确。

最重要的安全取舍是：文件系统（FS）是事实来源，VectorDB 是可以重建的派生索引。
系统宁可暂时漏掉搜索结果，也尽量避免索引指向已经不存在的源文件。不过，这是一条
操作排序原则，不代表跨 FS、VectorDB 和 QueueFS 的所有步骤具有原子性。

## 1. 正确的心智模型

结论是：这里的 transaction 更接近“受锁保护的多步操作”，而不是具有
commit、rollback 和 isolation level 的数据库事务。

`LockContext` 只负责：

1. 进入上下文时获取锁；
2. 获取失败时抛出 `LockAcquisitionError`；
3. 离开上下文时释放本次获得的锁；
4. 不吞掉业务异常。

它明确没有 undo、journal 或 commit 语义。若第二步写入失败，第一步已经完成的写入
不会由 `LockContext` 自动撤销。需要通过操作排序、显式清理、幂等重试、持久化队列
或操作专属恢复标记处理这种部分完成状态。

## 2. FS 是事实来源，VectorDB 是派生索引

结论是：当两个存储无法同时原子提交时，OpenViking 优先保住不可重建的 FS 源数据，
允许派生索引暂时不完整。

两种不一致的风险并不对称：

| 状态 | 用户影响 | 恢复方式 |
|---|---|---|
| FS 存在，索引缺失 | 搜索可能漏结果 | 从 FS 重新索引 |
| FS 已删除，索引仍存在 | 搜索返回不存在的资源 | 源数据无法从索引完整恢复 |

因此原文的 “Better to miss a search result than to return a bad one” 描述的是
**数据一致性取舍**，不是搜索的 `score_threshold` 或召回质量阈值。

这条原则也不等于“任何失败都保持 FS 和索引完全不变”。例如 `rm()` 先删除索引再删除
FS，可以保证“FS 被删除之前，索引清理已经返回成功”；但如果 VectorDB 内部按 URI 或
批次逐步删除，中途失败仍可能留下“FS 完整、索引部分删除”的状态。

## 3. EXACT 与 TREE 锁住什么

结论是：EXACT 保护一个具体路径，TREE 保护一个目录及整棵子树；是否冲突取决于两个
操作覆盖的路径集合是否相交。

### EXACT

`ExactPathLock(path)` 只保护 `path`。它适合单文件写入、单文件删除、sidecar 写回，
以及尚未创建的目标路径预留；例如锁住 `docs/a.md` 不会阻塞兄弟路径 `docs/b.md`。

锁文件位置取决于目标形态：

```text
现有目录路径:       {path}/.path.ovlock
文件或缺失路径:     {parent}/.exact.ovlock.<name>.<hash>
```

### TREE

`TreeLock(path)` 逻辑上保护 `path` 及其全部后代，但只在根路径写一个锁文件：

```text
docs/                    <- TREE(docs), physical lock here
├── .path.ovlock
├── a.md                 <- logically covered
└── guide/
    └── intro.md         <- logically covered
```

这适合目录删除、目录移动和从资源落盘到语义处理结束的生命周期保护。

### 冲突矩阵

| 当前要获取的锁 | 另一个同路径锁 | 另一个祖先 TREE | 另一个后代锁 | 无关兄弟 EXACT |
|---|---:|---:|---:|---:|
| EXACT | 冲突 | 冲突 | 不冲突 | 不冲突 |
| TREE | 冲突 | 冲突 | 冲突 | 取决于兄弟是否在该 TREE 下；不在则不冲突 |

同一个 owner 已持有祖先 TREE 时，可以复用该覆盖范围，不需要在后代再创建一个锁。

### 为什么并行计算、只让最后入队的版本写回

结论是：源文件用彼此独立的 EXACT 锁保持写入并行度，目录摘要则用版本淘汰旧结果，
最后只在 sidecar 写回阶段短暂串行化，避免用长时间 TreeLock 锁住整个目录。

1. `docs/a.md`、`docs/b.md`、`docs/c.md` 各持有自己的 EXACT 锁，互不阻塞；
2. 它们可以同时触发同一 `docs/` 的摘要任务，队列按 `coalesce_key` 递增版本；
3. 较早入队的任务即使已经开始计算，也会在写回前和取得 sidecar 锁后再次检查 stale 状态；
4. 只有同一 `coalesce_key` 下最后入队、即 `coalesce_version` 最大的任务可以在 `.overview.md`、`.abstract.md` 的 EXACT 锁内写回；任务何时开始或完成不决定新旧。

这把“昂贵但可并行的摘要计算”和“必须互斥的最终提交”分开：并发度更高，同时避免较早入队任务的摘要覆盖较晚入队任务的摘要，也避免最终写入在 sidecar 文件上交错。

## 4. 为什么先检查祖先、后代和同路径冲突

结论是：缺失路径上的 TreeLock 必须先确认排他权，再创建承载锁文件的目录；否则一次
失败的加锁也会留下可见目录，并可能改动另一个事务正在保护的树。

第一次添加资源时，`final_uri` 可能不存在，但 TreeLock 的物理文件需要放在：

```text
final_uri/.path.ovlock
```

所以获取顺序是：

```text
1. 检查同路径锁
2. 检查祖先 TreeLock
3. 检查同路径 ExactPathLock
4. 扫描后代锁
5. 确认无冲突后才创建 final_uri
6. 写 final_uri/.path.ovlock
7. 再次检查冲突并验证锁所有权
```

如果先创建目录再检查，失败的请求可能留下一个空 `final_uri`。后续代码可能把这个
锁载体误判为已经存在的资源。测试
`test_tree_blocked_by_ancestor_tree_does_not_create_missing_directory` 专门约束了这一点。

### 被后代 TreeLock 阻塞时

结论是：父路径不能在后代仍被处理时取得 TreeLock；当前操作等待、回收 stale lock，
或者以可重试的 busy 错误结束，后代操作继续执行。

例如：

```text
resources/project/              <- 当前操作想获取 TREE
└── docs/                       <- 另一个 owner 已持有 TREE
    └── .path.ovlock
```

父 TreeLock 会覆盖 `docs/`，所以允许两个锁同时成功将造成重叠写入。实际处理是：

1. 若后代锁已超过 `lock_expire`，删除 stale lock 并重新尝试；
2. 若后代锁仍有效且 `lock_timeout > 0`，按轮询间隔等待；
3. 若达到 timeout，`PathLockEngine.acquire_tree()` 返回 `False`；
4. `LockContext` 将失败转换成 `LockAcquisitionError`；
5. 资源添加路径再转换成
   `ResourceBusyError(conflict_type="path_busy", retryable=True)`。

默认 `lock_timeout=0.0`，所以默认行为是立即失败，而不是等待。

如果 `final_uri` 在文件系统中真正不存在，它通常也不可能已经拥有物理后代目录。
后代检查仍然有价值，因为“判断不存在”和“写锁”之间存在并发窗口，另一个操作可能
在窗口中创建目标或后代。

### 为什么写锁后还要复查

结论是：预检查只能缩小竞争窗口，不能消除 TOCTOU（检查时与使用时之间的状态变化）。

```text
owner A: 检查无冲突 ---------------- 写 A 的锁
owner B:        检查无冲突 ---------------- 写 B 的锁
```

因此写入锁文件后必须重新扫描同路径、祖先和后代。发现并发冲突时，代码比较 fencing
token 中的 `(timestamp, owner_id)`，让排序靠后的竞争者移除自己的锁并退避，避免双方
持续互相抢占。

需要注意：若锁系统为了写锁已经创建了空目录，后置复查失败时会移除自己的锁，但不会
回滚这个空目录。这也是为什么调用方判断“目标是否有真实内容”时，不能只看原始
`exists()`。

## 5. 四条主要写入流程

结论是：`rm`、`mv`、`add_resource` 和 `session.commit` 使用不同保护手段，因为它们
面对的失败窗口不同，不存在一套通用的自动回滚协议。

### `rm(uri)`

`rm` 通过“索引优先、FS 随后”避免产生指向已删除源文件的索引：

```text
选择 EXACT 或 TREE
  -> 获取锁
  -> 收集待删除 URI
  -> 删除 VectorDB 记录
  -> 删除 FS 路径
  -> 释放锁
```

- 文件使用 EXACT；
- 递归删除目录使用 TREE；
- VectorDB 删除抛错时，不继续删除 FS；
- FS 删除失败时，FS 仍存在，但索引可能已经删除，可以重新索引或重试删除；
- VectorDB 内部部分删除不是跨后端事务，不能由路径锁自动恢复。

### `mv(old_uri, new_uri)`

`mv` 使用 copy-first，而不是直接移动：

```text
锁住 source 和 destination
  -> copy source 到 destination
  -> 更新 VectorDB URI
  -> 删除 source
```

目录 source 使用 TREE，destination 使用 EXACT；文件两端都使用 EXACT。VectorDB
URI 映射按条目尽力更新；单个更新失败只记录 warning，不会中止移动。因此 source
仍可能被删除，而部分索引继续指向旧路径。遇到这类 warning 后，需要对 destination
重新索引或进行其他修复。

### `add_resource`

`add_resource` 把 TreeLock 持有到语义 DAG 和 embedding 全部完成，避免刚落盘的资源
在后台处理期间被 `rm` 删除。

```text
temp source
  -> 获取 final_uri TreeLock
  -> 把内容持久化到 final_uri
  -> enqueue semantic work，并传递 lifecycle lock handle
  -> DAG / embedding 完成
  -> 释放 TreeLock
```

后台处理会刷新锁。服务重启后，持久化的 QueueFS 消息仍在；若内存中的 handle 已丢失，
worker 会重新获取 TreeLock。

回调内部不应再用一个外层锁包住 `VikingFS.rm()` 或 `VikingFS.mv()`，因为这些方法会
自行加锁。重复包锁可能与内部锁产生冲突。

### `session.commit()`

`session.commit()` 把短时间的加锁状态转换与可恢复的慢速 LLM 工作分开：

```text
Phase 1: archive handoff（session ExactPathLock，无 LLM）
  -> 重新加载权威消息并拆分 archive / retained 集合
  -> 写 phase1.status=preparing 和恢复意图
  -> 持久化原始 archive
  -> enqueue SessionCommit QueueFS 任务
  -> 发布 retained 消息、session 元数据和 phase1.status=ready

Phase 2: summary + memory processing（QueueFS worker，锁外 LLM）
  -> 协调中断的 Phase 1
  -> 生成 archive summary
  -> 抽取并写入 memory / relations
  -> enqueue 并等待语义与索引任务
  -> 最后写 .done；终态失败写 .failed.json
```

当前恢复边界是 QueueFS 的 ACK 语义加 archive 标记：worker 退出时未 ACK 的任务会
重新投递；`.done` 阻止已完成任务重复执行，`.failed.json` 记录终态失败。旧版
RedoLog 路径只用于兼容恢复，不再是新 session commit 的主恢复机制。这里的安全重放
也不意味着 LLM 每次生成的文本逐字节完全相同。

## 6. 锁、QueueFS 与恢复标记的职责边界

结论是：锁处理并发互斥，QueueFS 负责已接受任务跨重启存活，archive/task 标记记录
可恢复阶段与终态；旧版 RedoLog 仅用于兼容恢复。这些机制不能互相替代。

| 机制 | 防御的问题 | 不保证什么 |
|---|---|---|
| Path lock | 同路径或重叠子树并发写 | 已完成步骤的自动回滚 |
| Fencing token | 并发获取与旧 owner 继续写 | 跨 VectorDB/FS 原子提交 |
| Stale cleanup | 进程崩溃后遗留的锁 | 立即恢复，必须等过期或重新获取 |
| QueueFS persistence | 已接受异步任务跨重启存活，未 ACK 任务重新投递 | 入队前发生的业务写入自动补偿 |
| Archive markers | 区分 Phase 1 准备/就绪以及 Phase 2 完成/终态失败 | 通用事务日志或 LLM 输出确定性 |
| Legacy RedoLog | 恢复旧版本遗留的 session-memory 标记 | 新 session commit 的主恢复机制 |

## 7. 可以依赖与不能依赖的保证

结论是：路径互斥和失败顺序是明确保证，跨存储原子性与通用回滚不是。

可以依赖：

- 同路径写入互斥；
- 祖先 TreeLock 阻塞后代写入；
- 后代锁阻塞祖先 TreeLock；
- stale lock 在新的 acquire 路径中被识别和回收；
- `LockContext` 离开时释放自己获得的锁；
- 默认冲突立即失败，错误可以向上层映射为 resource busy；
- `rm` 在 VectorDB 删除返回成功前不会主动删除现存 FS 目标。

不能直接推导：

- “任何 `rm` 失败都意味着 FS 和全部索引逐条保持原样”；
- “有路径锁就拥有跨 FS、VectorDB、QueueFS 的 ACID 原子性”；
- “异常退出会撤销上下文内已经完成的写入”；
- “QueueFS 会自动补偿所有入队前的业务写入”；
- “旧版 RedoLog 是当前 session commit 的主恢复机制”；
- “幂等重跑会产生逐字节相同的 LLM 输出”；
- “锁文件不存在就证明没有并发工作”，因为还需检查祖先和 sidecar exact locks。

## 8. 阅读源码的推荐顺序

结论是：先读锁语义，再读具体业务流程，最后用测试确认边界，最容易建立完整模型。

推荐顺序是：原概念文档 → `lock_context.py` → `path_lock.py` →
`lock_manager.py` / `lock_lease.py` → `viking_fs.py` 的 `rm()`、`mv()` →
`resource_processor.py` Phase 3.5 → `semantic_lock.py` → `session.py` →
`session_commit_queue.py` / archive marker 处理 → `redo_log.py`（旧版兼容）。

最后阅读 `tests/transaction/test_path_lock.py` 和
`tests/transaction/test_concurrent_lock.py`，重点搜索 missing directory、ancestor、
descendant、stale、no-wait 和 mutual exclusion 等测试名。
