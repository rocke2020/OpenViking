# OpenClaw + OpenViking 多租户学习笔记

## 目标

当你有多个 OpenClaw 实例时，常见需求是：

- 某些 OpenClaw 实例属于团队 A
- 另一些 OpenClaw 实例属于团队 B
- 每个实例仍然可以使用“一个用户身份 + 一个 user key”的简单模型

这时不需要把每个 OpenClaw 实例都当成一个独立的租户入口，而是把“team”映射为不同的 OpenViking `account`，再给每个实例配置该 account 下的 user key。

## 核心思路

### 1. 先把团队映射成 account

每个团队对应一个 OpenViking `account`：

- `team-a` 代表团队 A
- `team-b` 代表团队 B

这样不同团队的数据边界就被 `account_id` 隔开。

### 2. 每个 OpenClaw 实例绑定一个 account 下的 user key

一个 OpenClaw 实例仍然可以使用一个 user key；
只不过这个 user key 不是“全局共享”的，而是属于某个特定 account 下的用户。

例如：

- OpenClaw 实例 A 使用 `team-a` 账户下的 user key
- OpenClaw 实例 B 使用 `team-b` 账户下的 user key

这样服务端就会把它们分别解析到不同的 `account_id` 和 `user_id`。

## 实际实现步骤

### Step 1：创建不同 account

```bash
curl -X POST http://localhost:1933/api/v1/admin/accounts \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <root-key>" \
  -d '{"account_id": "team-a"}'

curl -X POST http://localhost:1933/api/v1/admin/accounts \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <root-key>" \
  -d '{"account_id": "team-b"}'
```

### Step 2：在每个 account 下创建用户并生成 key

```bash
curl -X POST http://localhost:1933/api/v1/admin/accounts/team-a/users \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin-or-root-key>" \
  -d '{"user_id": "oc-a", "role": "user"}'

curl -X POST http://localhost:1933/api/v1/admin/accounts/team-b/users \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin-or-root-key>" \
  -d '{"user_id": "oc-b", "role": "user"}'
```

然后为每个用户生成对应的 user key。

### Step 3：给每个 OpenClaw 实例配置对应 key

```bash
openclaw config set plugins.entries.openviking.config.mode remote
openclaw config set plugins.entries.openviking.config.baseUrl "http://your-server:1933"
openclaw config set plugins.entries.openviking.config.apiKey "<team-a-user-key>"
```

另一台实例配置为：

```bash
openclaw config set plugins.entries.openviking.config.mode remote
openclaw config set plugins.entries.openviking.config.baseUrl "http://your-server:1933"
openclaw config set plugins.entries.openviking.config.apiKey "<team-b-user-key>"
```

## 结果

这样就实现了：

- 一个 OpenClaw 实例仍然使用一个 user key
- 不同实例可以属于不同团队
- 通过不同 `account` 把团队边界隔开
- 同一 account 内的资源仍然可以共享，但不同 account 之间不会互通

## 什么时候适合这样做

适合以下场景：

- 每个 OpenClaw 实例固定绑定一个团队身份
- 你希望保持集成简单，不想为每个实例维护复杂的租户逻辑
- 你想让“团队”这个概念映射为 OpenViking 的 `account`

## 什么时候用其他模式

如果你要做的是“一个平台服务为大量终端用户提供能力”，那更适合 Vikingbot 这类模式：

- 由 root key 管理多个用户
- 在服务端统一维护 account / user 生命周期
- 通过缓存和注册机制为不同终端用户分发身份
