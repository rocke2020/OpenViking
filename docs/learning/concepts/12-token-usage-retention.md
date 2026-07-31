# OpenViking Token 使用量、保留策略与 Studio 展示学习笔记

> 相关文档：[Metrics](../../en/concepts/12-metrics.md)

## TL;DR

OpenViking 的 Token 统计分为三个不同层次：`/metrics` 提供进程内 Prometheus
计数器，Usage/Audit SQLite 保存可跨进程查询的聚合历史，Studio Home 再从这些持久化
数据中选择一个时间窗口展示。`usage_retention_days=0` 只表示不再按天裁剪 Usage/Audit
数据，不会把 `/metrics` 变成历史数据库，也不会自动把 Studio 当前固定的 14 天图表
改成全历史。

本机在 `~/.openviking/ov.conf` 中把
`server.observability.usage_audit.usage_retention_days` 设置为 `0`。这会保留从现在起写入及
数据库中当前仍存在的全部 Usage/Audit 聚合记录，但之前按照 14 天策略已经删除的记录
无法自动恢复。因此，UI 和 API 应使用“保留历史”而不是“生命周期历史”描述这组数据。

## 1. 三个统计层次

结论是：Prometheus 指标、Usage/Audit 存储和 Studio 图表解决的是不同问题，不能用其中
一层的配置推断另外两层的查询范围。

| 层次 | 数据来源 | 生命周期 | 主要用途 |
|---|---|---|---|
| `/metrics` | 进程内模型累计计数和事件 | 跟随当前服务进程和指标注册表 | Prometheus 抓取、告警、短期趋势 |
| Usage/Audit SQLite | 模型调用事件的小时级聚合 | 由 `usage_retention_days` 控制 | 持久化用量查询、Console 数据源 |
| Studio Home | Console API 返回的数据 | 由前端选择的查询范围控制 | 人类可读的摘要和趋势图 |

`openviking_model_tokens_total` 是 Prometheus Counter。Collector 从模型实例的进程内累计
值计算正增量；只有增量大于 `0` 时才创建或增加对应 series。因此，刚启动且尚无模型
Token 使用时，下面的命令可能只看到 calls，或者完全看不到 tokens series：

```bash
curl -s http://localhost:1933/metrics \
  | rg 'openviking_model_(calls|tokens)_total'
```

即使该指标已经出现，它也不等于 SQLite 中的全部保留历史。服务重启会重建进程内
Collector 状态，而 Usage/Audit SQLite 可以继续保留之前的聚合记录。

## 2. `usage_retention_days` 的准确语义

结论是：这个配置只控制 Usage/Audit 的 Token、检索和上下文写入聚合表按天裁剪；
默认值是 `14`，`0` 表示跳过按天裁剪。

本机配置如下：

```json
{
  "server": {
    "observability": {
      "usage_audit": {
        "usage_retention_days": 0
      }
    }
  }
}
```

| 配置值 | 存储语义 | “全部保留历史”的上限 |
|---:|---|---|
| `14` | 每个 account 保留截至其最新事件日期的 14 个日历日窗口 | 最多 14 天 |
| `N > 0` | 每个 account 保留截至其最新事件日期的 N 个日历日窗口 | 最多 N 天 |
| `0` | 不执行 Usage/Audit 按天裁剪 | SQLite 中当前存在及以后写入的全部记录 |

裁剪发生在新一批 Usage/Audit 事件写入时，并按 account 的最新事件日期计算 cutoff。
把值从 `14` 改成 `0` 后，系统只会停止后续裁剪；它没有备份恢复或历史回填机制。

该字段也不控制请求审计日志。审计日志另有
`audit_retention_days` 和 `audit_retention_per_account` 两个配置。

## 3. 为什么 Studio 仍然只显示 14 天

结论是：Studio 当前明确请求最近 14 天，而不是自动读取存储保留范围，所以
`usage_retention_days=0` 不会改变图表。

当前前端行为由以下代码决定：

- [`TOKEN_SERIES_DAYS = 14`](../../../web-studio/src/routes/home/-constants/dashboard.ts)
- [`fetchConsoleTokenSeries()`](../../../web-studio/src/routes/home/-lib/api.ts) 根据该常量生成
  `start_date` 和 `end_date`
- [`route.tsx`](../../../web-studio/src/routes/home/route.tsx) 使用
  `last-14-days` query key
- 中英文文案都明确写着“最近 14 天”

后端 `GET /api/v1/console/tokens` 也要求调用方传入 `start_date` 和 `end_date`。因此，
存储层可能已经保留超过 14 天的数据，但当前 Studio 不会请求或展示这些旧记录。

## 4. 两个 Studio 功能的关系和优先级

结论是：“全部保留历史汇总”和“All time”范围选项应该共享同一套持久化数据语义，
但汇总卡片优先级更高，因为它直接回答累计用了多少 Token。

建议顺序如下：

1. **P0：全部保留历史 Token 汇总**
   - 展示 `total`，并保留 VLM input、VLM output、Embedding input 的拆分；
   - 文案使用 “Total retained token usage” / “全部保留历史 Token 用量”；
   - 返回实际最早和最晚可用日期，避免把已裁剪的数据误称为生命周期总量。
2. **P1：趋势图范围选择**
   - 提供 `14 days | 30 days | All retained history`；
   - 默认继续使用 14 天，避免长历史降低首页可读性；
   - 全历史增长后应支持月级聚合或降采样，而不是永久返回每天一个点。

这两个功能不应只在 `usage_retention_days=0` 时出现。配置为 `14` 时，“全部保留历史”
自然就是当前仍保留的最多 14 天；配置为 `0` 时，它才会随着运行时间持续增长。
换句话说，retention 是数据政策，不是 Studio feature flag。

当前 Token series API 会为请求范围内的每一天补一个零值记录。因此不能用
`start_date=1970-01-01` 模拟 All time；历史变长后，这会产生大量无意义的零点。更合适
的后端能力是直接返回保留范围和聚合汇总，再让趋势 API 查询真实的最早日期并选择合适
bucket。

## 5. 身份范围会影响累计值

结论是：同一个 SQLite 数据库的全库总数不一定等于 Studio 当前用户看到的总数，因为
Console API 会根据请求身份自动缩小查询范围。

- root/admin 查询聚合整个当前 account；
- 普通 user 只查询当前 `user_id`；
- 不同 account 的记录不会合并到同一个 Studio 用户视图。

因此，直接执行数据库全表 `SUM(token_count)` 得到的是数据库范围总数，不能直接拿来
校验普通用户的 Studio 卡片。正确校验必须同时匹配 `account_id` 和 `user_id`。

## 6. 本机只读检查

结论是：分别检查配置、Prometheus 指标和 SQLite，才能确认保留政策、当前进程统计和
持久化历史三个层次。

检查 retention 配置：

```bash
jq '.server.observability.usage_audit.usage_retention_days' \
  ~/.openviking/ov.conf
```

检查当前进程指标：

```bash
curl -s http://localhost:1933/metrics \
  | rg 'openviking_model_(calls|tokens)_total'
```

检查 SQLite 中每个身份范围的保留 Token 总数：

```bash
sqlite3 ~/.openviking/data/_system/usage_audit/usage_audit.sqlite3 '
SELECT
  account_id,
  user_id,
  MIN(date_utc) AS first_date,
  MAX(date_utc) AS last_date,
  SUM(token_count) AS retained_tokens
FROM usage_token_hourly
GROUP BY account_id, user_id
ORDER BY account_id, user_id;
'
```

这个查询是只读的。`retained_tokens` 表示数据库里仍存在的累计值，不保证包含启用
Usage/Audit 之前或旧 retention 策略已经删除的数据。

## 7. 核心心智模型

结论是：先问“数据存多久”，再问“API 查多大范围”，最后问“UI 展示哪一段”，才能正确
理解 Token 数字。

```text
模型调用
  ├─> 进程内累计值 ─> /metrics
  └─> Usage/Audit 事件 ─> SQLite
                         │
                         ├─ retention 决定保留多久
                         ├─ request identity 决定能看谁
                         └─ Studio range 决定展示哪一段
```

最终定义可以记成一句话：

> `usage_retention_days=0` 不再按天裁剪当前及后续写入成功的 Usage/Audit 聚合记录；
> “累计历史总量”和“All time”仍需要 Studio/API 明确查询并展示全部保留数据。

## 相关源码

结论是：以下文件分别是 retention、身份范围、Console API、Studio 时间窗口和 Prometheus
模型计数的权威实现。

- [Usage/Audit 配置](../../../openviking/server/config.py)
- [SQLite retention 与 Token 查询](../../../openviking/observability/usage_audit/sqlite_store.py)
- [身份范围与 Console 查询服务](../../../openviking/observability/usage_audit/api_service.py)
- [Console API 路由](../../../openviking/server/routers/console.py)
- [模型用量 Prometheus Collector](../../../openviking/metrics/collectors/model_usage.py)
