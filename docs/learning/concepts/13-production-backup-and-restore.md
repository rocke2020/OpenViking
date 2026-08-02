# OpenViking 生产环境周期备份与恢复建议

> 相关文档：[OVPack Import and Export](../../en/guides/09-ovpack.md) ·
> [Storage Architecture](../../en/concepts/05-storage.md) ·
> [Multi-Write Storage](../../en/concepts/14-multi-write-storage.md) ·
> [Data Encryption](../../en/concepts/10-encryption.md)

## TL;DR

OpenViking 的生产备份不能只复制一个目录或只依赖 Multi-Write。AGFS 保存权威内容，
VectorDB 是派生索引，workspace 还包含 QueueFS 等运行状态；配置、凭据和加密 Root Key
通常位于数据目录之外。推荐同时保留 OVPack、后端原生快照、配置与密钥备份，并定期在
隔离环境恢复验证。

最重要的恢复原则是：**先恢复到新的目录、bucket、数据库或集群并完成验收，再切换流量；
不要直接覆盖唯一的生产副本。** `ov restore` 默认冲突策略是 `fail`，生产恢复应显式保留
这个安全行为。

## 1. 先区分需要保护的五类资产

结论是：AGFS 丢失意味着源数据丢失；VectorDB 通常可以重建；QueueFS、配置和密钥决定
系统能否完整继续运行。备份策略必须分别覆盖这些资产。

| 资产 | 常见位置或后端 | 恢复意义 | 推荐保护方式 |
|---|---|---|---|
| AGFS 权威内容 | 本地 `workspace/viking`，或 S3/MinIO bucket/prefix | 资源、记忆、技能、Session 等源内容 | OVPack + 文件系统/对象存储原生快照 |
| VectorDB 派生索引 | 本地 `workspace/vectordb`，或 Qdrant、openGauss、VikingDB 等 | 检索性能；可从 AGFS 重建，但可能影响 RTO | 原生快照，或明确接受重建时间 |
| 运行与内部状态 | `workspace/_system/`、QueueFS、Usage/Audit 等 | 在途任务、审计和运行状态 | 完整 workspace 的应用一致性快照 |
| 配置与凭据 | `ov.conf`、环境变量、Secret Manager、对象存储和 VectorDB 凭据 | 决定目标环境如何连接和解释数据 | 每次变更后版本化备份，秘密单独加密保存 |
| 加密 Root Key | 本地 key file、Vault 或 KMS | 解密物理 AGFS 副本的必要条件 | 独立、受控、至少双份的密钥灾备 |

本地默认布局来自同一个 `storage.workspace`：AGFS 使用 `workspace/viking`，本地
VectorDB 使用 `workspace/vectordb`，QueueFS SQLite 默认使用
`workspace/_system/queue/queue.db`。如果 AGFS 或 VectorDB 使用外部服务，完整恢复还必须
覆盖对应的 bucket、集群或集合；只备份 workspace 不再足够。

普通 PostgreSQL/pgvector 目前不是内置 VectorDB 后端；`opengauss` 适配器也不能据此
视为普通 PostgreSQL 支持。自定义 PostgreSQL 适配器的备份和兼容性由部署方负责。

## 2. OVPack 是逻辑备份，不是完整基础设施快照

结论是：`ov backup` 是首选的可移植内容备份，但它只覆盖公开的 `resources` 和 `user`
scope；内部运行状态、配置、密钥和后端级恢复信息不在其中。生产环境应把 OVPack 作为
灾备的一层，而不是唯一一层。

OVPack 包含 `viking://resources` 和 `viking://user`（包括用户 Session）、文件内容、语义
sidecar、可移植索引标量字段，以及可选的纯 dense vector snapshot。它不包含 QueueFS、
临时上传、锁、watch control、`.relations.json`、OpenViking 配置、凭据、Root Key、外部
VectorDB 或 bucket 版本历史。

默认不使用 `--include-vectors`，让恢复重新计算向量。只有 pure dense 索引、一致性检查
通过、embedding 配置兼容且缩短 RTO 很重要时才包含向量；hybrid index 当前不支持。

还应把 `.ovpack` 当作明文敏感备份管理。导出实现通过 VikingFS 的
`read_file_bytes()` 读取内容，而 VikingFS 对上层提供透明解密；因此不能假设 OVPack
自动继承 AGFS 后端上的密文保护。备份文件应使用严格权限、加密传输和独立的备份端加密。

## 3. 推荐的生产周期与保留基线

结论是：备份周期必须不大于业务 RPO，恢复演练耗时必须满足 RTO。下面是起始建议，
不是 OpenViking 默认值，应按数据量、合规期限和向量重建耗时调整。

| 项目 | 建议起始周期 | 建议保留 | 说明 |
|---|---:|---:|---|
| AGFS 原生快照或对象版本 | 每 1 小时 | 48 小时点 + 30 日点 | 保护权威内容；对象存储同时启用版本化 |
| 完整本地 workspace 应用一致性快照 | 每 4 小时 | 14 天 | 覆盖本地 AGFS、VectorDB、QueueFS 和内部 SQLite |
| OVPack 逻辑备份 | 每 24 小时 | 7 日 + 4 周 + 12 月 | 用于跨后端、跨实例的内容恢复 |
| 外部 VectorDB 原生快照 | 每 24 小时 | 至少覆盖目标 RPO | 如果可在 RTO 内重建，可以降低频率但要记录决定 |
| 配置、Secret 引用与 Root Key 灾备 | 每次变更后立即执行 | 至少两个独立安全副本 | 密钥丢失不能靠数据副本补救 |
| 隔离环境恢复抽查 | 每月 | 保留演练报告 | 验证最近 OVPack 和至少一种物理快照 |
| 完整灾备演练 | 每季度及重大升级前 | 保留审计记录 | 从空环境恢复并测量真实 RPO/RTO |

建议遵循 `3-2-1-1-0`：至少三份数据、两个故障域、一份异地、一份不可变或离线副本，
并以零未验证恢复为目标。文件非空或 checksum 正确都不能代替实际恢复。

## 4. 周期性备份操作步骤

结论是：日常任务应先生成 OVPack，再由基础设施分别保护 workspace、对象存储和外部
VectorDB，最后把 checksum、配置版本和恢复点写入备份清单。不要在服务持续写入时直接
递归复制 SQLite 或本地 VectorDB 目录。

### 4.1 生成每日 OVPack

以下脚本展示安全的最小流程；路径和调度器应按部署环境调整：

```bash
set -euo pipefail
umask 077

backup_root=/secure-backups/openviking
backup_timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_file="${backup_root}/openviking-${backup_timestamp}.ovpack"

mkdir -p "$backup_root"
ov backup "$backup_file"
openssl dgst -sha256 "$backup_file" > "${backup_file}.sha256"
```

任务成功条件包括：命令退出码为 `0`、文件可读、传输后 checksum 匹配、不可变副本落盘。
checksum 不能代替 OVPack manifest 校验和实际恢复。

不要在脚本尾部直接用 `rm` 清理旧备份；应由可审计的生命周期策略管理准确的
bucket/prefix。

### 4.2 生成基础设施原生快照

本地部署应停止新写入并暂停后台 worker，再对**整个 workspace** 创建应用一致性快照。
若平台只能提供 crash-consistent snapshot，应先停止 OpenViking；不要逐个复制运行中的
`queue.db` 和 VectorDB 文件。

S3/MinIO 应对实际 bucket/prefix 启用版本化和不可变保留，并复制到不同故障域。恢复时
先写入新 bucket/prefix，验证后再切换配置。

外部 VectorDB 应使用原生 snapshot/export，并记录 schema、距离算法、维度和 embedding
配置。本地 `local`/`cuvs` 位于 workspace；外部后端需要单独备份。

### 4.3 备份配置、凭据和密钥

配置备份必须记录 OpenViking 版本、AGFS bucket/prefix、VectorDB collection、embedding
配置和加密 provider。Secret 实际值由 Secret Manager、Vault 或 KMS 单独保护。

本地 provider 的 Root Key 必须单独加密备份。Vault/KMS 还要保护 key version、服务状态
和访问策略；只保存 key name 不够。没有正确 Root Key 的 AGFS 密文无法恢复。

### 4.4 监控 Multi-Write，但不要把它当备份

Multi-Write 只复制启用后的 AGFS 新写入，不回填历史，也不保护 VectorDB、QueueFS、配置
或 Root Key。Async 成功只确认 primary 写入，应周期检查：

```bash
ov system backend sync-status viking://resources
ov system backend sync-status viking://user
```

若某个 `(path, backend)` 已 quarantine，先修复目标后端，再对确认过的 URI 人工重试：

```bash
ov system backend sync-retry viking://resources
```

人工重试不是历史全量 backfill，也不能替代 OVPack 和后端原生快照。

## 5. OVPack 恢复步骤

结论是：把 OVPack 恢复到没有公开 scope 的隔离实例，显式使用 `fail`，向量完成后再
验收检索。只有经过审批且已有目标快照时，才考虑 `--on-conflict overwrite`。

1. 创建隔离实例，使用新的 workspace、bucket/prefix 和 VectorDB collection。
2. 安装兼容版本，恢复配置、Secret 和目标环境加密 provider。
3. 校验外部 checksum，并保留原文件只读副本。
4. 对最可移植的恢复路径，强制重建向量：

   ```bash
   ov restore ./openviking-YYYYmmddTHHMMSSZ.ovpack \
     --on-conflict fail \
     --vector-mode recompute
   ```

5. 如果备份明确包含兼容的 pure-dense snapshot，可以使用：

   ```bash
   ov restore ./openviking-YYYYmmddTHHMMSSZ.ovpack \
     --on-conflict fail \
     --vector-mode auto
   ```

6. 检查服务状态和恢复后的公开树：

   ```bash
   ov status
   ov tree viking://resources
   ov tree viking://user
   ```

7. 等待 vectorization 完成，再执行内容读取、一致性和业务检索验收。

`fail` 也是实现默认值，显式写出便于审计。`overwrite` 不是原子回滚：实现先删除冲突
scope，再逐项写入；中途故障可能留下不完整目标。应恢复到空目标并在验收后切换。

## 6. 物理/基础设施恢复步骤

结论是：物理恢复必须让 AGFS、workspace 内部状态和 VectorDB 对应同一个可解释的恢复
时间点。若 VectorDB 快照与 AGFS 不匹配，应创建新的索引/collection 并从 AGFS 重建，
而不是把不确定的旧索引直接投入生产。

1. 冻结现场，保存日志、版本、配置和存储快照，不在原位置破坏性修复。
2. 准备新目录、卷、bucket/prefix 和 VectorDB collection/index 或恢复集群。
3. 恢复匹配的 OpenViking 版本、配置、Secret 和 Root Key；物理加密数据必须使用原来
   可解密它的 Root Key/KMS 状态。
4. 把 AGFS 和 workspace 恢复到新目标；全部文件和内部 SQLite 就绪前不要启动多实例。
5. 恢复同一恢复窗口的 VectorDB 原生快照；若无法证明一致，保留旧快照只读，创建新的
   索引并从 AGFS 重建。
6. 仅启动一个隔离实例，等待必要的 queue recovery、vectorization 或 index build 完成。
7. 完成第 7 节验收后再切换流量；保留旧环境直至回退窗口结束。

## 7. 恢复验收标准

结论是：服务能启动、文件非空或目录数量相近都不能证明恢复成功。验收必须验证源内容、
身份边界、向量一致性、检索和后台状态，并记录恢复耗时。

每次演练至少完成以下检查：

- `ov status` 健康，日志中没有持续的存储、解密、QueueFS 或 VectorDB 错误；
- `viking://resources` 和 `viking://user` 的预期顶层目录存在；
- 读取多个代表性文件并比对预期 hash，而不是只看文件名；
- 普通用户仍只能看到其身份范围内的数据，共享资源的访问边界符合预期；
- 对代表性树执行 `ov system consistency <uri>`，确认没有缺失索引记录；
- 使用固定检索问题验证预期结果可被召回，并确认不是仅由缓存返回；
- 抽查 Session、memory、skill 和资源附件等不同数据类型；
- Multi-Write 部署检查 `sync-status`，不存在未解释的 pending 或 quarantined 项；
- 记录恢复点、数据损失窗口和恢复耗时，确认满足 RPO/RTO。

演练应使用隔离账号、独立路径和无真实流量的 endpoint；清理由备份平台受控执行。

## 8. 常见故障与正确恢复来源

结论是：不同资产需要不同恢复来源。优先保护 AGFS 和密钥；VectorDB 可重建，但
QueueFS 在途状态和历史审计不能从 OVPack 自动补回。

| 故障 | 首选恢复来源 | 关键注意事项 |
|---|---|---|
| 本地 AGFS 丢失或损坏 | 完整 workspace 快照或 OVPack | VectorDB 不能可靠反推全部源内容 |
| S3/MinIO 对象误改 | bucket version/immutable replica，或 OVPack | 恢复到新 prefix 后验证，再切换配置 |
| VectorDB 丢失 | 原生 snapshot，或从 AGFS 重建 | 重建期间源内容仍在，但检索可能不完整 |
| QueueFS/内部 SQLite 丢失 | 应用一致性 workspace 快照 | OVPack 不恢复在途后台任务和 Usage/Audit 历史 |
| Root Key/KMS 状态丢失 | 独立密钥灾备 | 加密 AGFS 副本本身不能替代密钥 |
| Multi-Write backup 落后 | `sync-status`、修复后 `sync-retry`、必要时 OVPack 全量迁移 | quarantine 只影响对应 path/backend，不代表全系统备份可用 |
| embedding 配置变化 | OVPack `--vector-mode recompute` | 不要强制载入不兼容的 dense snapshot |

## 9. 上线前检查清单

结论是：没有负责人、目标和定期演练的备份方案不可运营。每项都应有 owner、告警和最近
成功时间。

- [ ] 已定义每类数据的 RPO、RTO 和保留期；
- [ ] OVPack 调度任务会对失败退出码告警；
- [ ] AGFS 本地卷或 S3/MinIO bucket 已启用独立快照/版本化；
- [ ] 外部 VectorDB 有原生 snapshot，或已测量并接受全量重建时间；
- [ ] 完整 workspace 使用应用一致性快照，而不是运行时目录复制；
- [ ] `ov.conf`、版本、embedding 配置和 schema 已进入备份清单；
- [ ] Secret 与 Root Key 存在独立、加密、受访问审计的灾备副本；
- [ ] 至少一份备份位于不同账号、集群或地域，并设置不可变保留；
- [ ] Multi-Write 的 `sync-status` 有周期监控和 quarantine 告警；
- [ ] 最近的隔离恢复已验证真实内容、检索和身份边界；
- [ ] `--on-conflict overwrite` 有单独审批和目标端预快照要求；
- [ ] 恢复切换与回退步骤已经由当班人员实际演练。

## 相关源码

结论是：以下文件定义存储平面、默认路径、OVPack 行为、VectorDB 后端和 Multi-Write
接口，是维护本建议时的权威实现。

- [StorageConfig 与 workspace 路径解析](../../../openviking_cli/utils/config/storage_config.py)
- [AGFS、QueueFS 与 Multi-Write 配置组装](../../../openviking/utils/agfs_utils.py)
- [VectorDB backend 配置](../../../openviking_cli/utils/config/vectordb_config.py)
- [VectorDB adapter factory](../../../openviking/storage/vectordb_adapters/factory.py)
- [本地 VectorDB 路径实现](../../../openviking/storage/vectordb_adapters/local_adapter.py)
- [OVPack backup/restore 实现](../../../openviking/storage/ovpack/operations.py)
- [OVPack 默认冲突与向量策略](../../../openviking/storage/ovpack/format.py)
- [Filesystem 为 source of truth 的事务说明](../../en/concepts/09-transaction.md)
- [Multi-Write 系统 API](../../en/api/07-system.md)
