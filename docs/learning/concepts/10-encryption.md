# OpenViking 多租户加密隔离学习笔记

> 原文：[Data Encryption](../../en/concepts/10-encryption.md)  
> 相关概念：[Multi-Tenant](../../en/concepts/11-multi-tenant.md)

## TL;DR

OpenViking 的 `account` 是外层租户边界，可以理解为一个组织、团队或客户空间；
一个 `account` 可以包含多个 `user`。系统整体同时使用 account 和 user 两级隔离，
但加密文档中的 “Multi-Tenant Isolation” 特指 **account 级的密码学隔离**：
每个 account 使用独立的 Account Key，同一 account 内的不同 user 并没有各自独立的
Account Key。

## Account 隔离与 User 隔离不是同一层

| 边界 | 心智模型 | 主要作用 |
|---|---|---|
| `account_id` | 组织、团队、客户或 workspace | 隔离不同租户的全部数据 |
| `user_id` | account 内的用户 | 隔离用户私有的 memory、session、skill 和 user resource |

因此，“多租户隔离在 account 级还是 user 级”的准确回答是：**两级都有，但职责不同**。

- account 是真正的 tenant。不同 account 的底层存储路径带有不同的
  `/local/{account_id}/` 前缀，检索也会按 `account_id` 过滤。
- user 是 account 内的二级访问边界。普通用户只能访问自己的
  `viking://user/{user_id}/...` 空间。
- `viking://resources/...` 是 account 内共享资源；同一 account 的多个用户可以访问。
- 用户的 memory、session、skill 和 user resource 默认不与同一 account 的其他用户共享。

例如：

```text
account: acme
├── resources/              # Alice 和 Bob 共享
├── user/alice/
│   ├── memories/           # Alice 私有
│   ├── sessions/
│   └── skills/
└── user/bob/
    ├── memories/           # Bob 私有
    ├── sessions/
    └── skills/
```

## 为什么加密文档只强调 Account

加密层使用三层密钥结构：

```text
Root Key
  -> Account Key (每个 account 一个)
    -> File Key (每次写入随机生成)
```

Account A 的 Account Key 无法解密 Account B 的文件，因此即使多个 account 的密文存放在
同一个 AGFS 实例中，仍然具有 account 级的密码学隔离。

同一 account 内的用户文件虽然各自使用随机 File Key，但这些 File Key 都由该 account 的
Account Key 保护。因此，**user 之间的隔离主要由 URI namespace、请求身份和权限检查实现，
不是由彼此独立的 Account Key 实现**。

可以把完整模型记成一句话：

> tenant 是 account；大多数个人上下文还会在 user 级进一步隔离。加密隔离 account，
> namespace 和授权隔离 account 内的 user。
