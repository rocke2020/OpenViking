# Fast Token Estimation — 估算逻辑

> OV 如何在不调用真实 tokenizer 的前提下，单次线性扫描估算 token 数，以及为什么 server / client 用了不同口径。

## TL;DR

OV 用纯算术估算 token，**不加载词表、不做 BPE、无依赖**，单次 O(n) 扫描。决定 commit 的关键路径（`pending_tokens`）始终用 server 的 CJK-aware 口径；client 内部两份估算器口径分歧是历史包袱，不影响归档一致性。

## 三套实现

| 位置 | 实现 | CJK-aware | 作用 |
|---|---|---|---|
| server `openviking/utils/token_estimation.py` | 码点加权 1.5 / 0.25 / 2.0 | ✅ | `Message.estimated_tokens` → `Session.pending_tokens`，权威值 |
| client `examples/claude-code-memory-plugin/scripts/lib/profile-inject.mjs:72` | `1.5 × CJK + chars/4` | ✅ | 注入 profile 前的本地预算截断 |
| client `examples/claude-code-memory-plugin/scripts/auto-recall.mjs:205` | 纯 `chars/4` | ❌ | 召回行预算截断（openclaw 移植件） |

## server 估算器

`openviking/utils/token_estimation.py`：

```python
def _code_point_weight(code_point):
    if _is_cjk_code_point(code_point): return 1.5   # CJK / 日韩
    if code_point > 0xFFFF:          return 2.0    # 补充平面（emoji 等）
    return 0.25                                     # ASCII / 拉丁 ≈ chars/4

def estimate_text_tokens(text):
    return math.ceil(sum(_code_point_weight(ord(c)) for c in text))
```

权重依据：1.5 token/char 是 `cl100k_base` 在中文上的经验均值，Claude tokenizer 同量级；补充平面（emoji 等）按 2.0；ASCII/拉丁按 0.25（标准 chars/4 启发式）。

`_is_cjk_code_point` 覆盖区段：CJK 统一（`0x4E00-0x9FFF`）、扩展 A（`0x3400-0x4DBF`）、兼容（`0xF900-0xFAFF`）、扩展 B（`0x20000-0x2EBEF`）、平假名 / 片假名、谚文、全角 ASCII、CJK 符号。

调用链：
- `openviking/message/message.py:67` — `Message.estimated_tokens` 用它
- `openviking/session/session.py:459` — `_rebuild_pending_tokens` 把所有 message 的 `estimated_tokens` 求和，得到 `pending_tokens`
- `auto-capture.mjs:632` — client 直接读 server 返回的 `meta.pending_tokens`，不自己算

## client 估算器

### profile-inject.mjs（CJK-aware）

`examples/claude-code-memory-plugin/scripts/lib/profile-inject.mjs:72`：

```js
export function estimateTokens(text) {
  if (!text) return 0;
  let cjk = 0;
  for (let i = 0; i < text.length; i++) {
    if (text.charCodeAt(i) >= 0x3000) cjk++;
  }
  const other = text.length - cjk;
  return Math.ceil(cjk * 1.5 + other / 4);
}
```

注释（`:56-71`）说明为什么必须 CJK-aware：标准 chars/4 对中文低估 4-6×，会让「5000 token 预算」实际塞进去 30000 真实 token。1.5/char 宁可高估 10-20%，是预算方向的安全侧。

### auto-recall.mjs（chars/4，无 CJK 校正）

`examples/claude-code-memory-plugin/scripts/auto-recall.mjs:205`：

```js
function estimateTokens(text) {
  return text ? Math.ceil(text.length / 4) : 0;
}
```

注释标注 `// chars/4 heuristic (openclaw-plugin/index.ts:1812)` —— 这是 openclaw-plugin 的逐字移植，openclaw 英文优先，chars/4 够用；移植时为避免行为漂移没改。

## 为什么 server / client 口径可以不同

### 关键路径不靠 client 估算器

`auto-capture.mjs:632,636`：
```js
pendingTokens = Number(meta?.pending_tokens || 0);   // 读 server 数字
if (pendingTokens >= cfg.commitTokenThreshold) { commitSession(...); }
```

commit 阈值（20000）触发与否，**始终用 server 的 CJK-aware 数字**。CC 和 Pi 共用同一口径，归档节奏一致，client 内部分歧不影响这一点。

### client 估算器只管本地截断

两份 client 估算器都是「塞进 agent context 之前」的粗筛：
- profile-inject.mjs：profile block 截断
- auto-recall.mjs：召回行截断

agent 自己的 tokenizer 才是最终仲裁者，client 估算器 ±20% 不破坏功能。

### server 必须 CJK-aware

OV 是火山引擎（字节）项目，中文是主要场景；`pending_tokens` 是落库的权威值，对中文低估 4-6× 会直接破坏 commit 节奏。

### client 的 chars/4 是历史包袱

openclaw 移植件，只影响召回行截断点，不影响 commit 也不影响落库，没动力重写。profile-inject.mjs 是后写的，作者意识到中文场景必须校正，所以重做了 CJK-aware 版本，并在注释里点名「其他地方还在用 chars/4」。

## 速度特性

- **O(n) 纯整数算术**：1 MB 文本毫秒级。
- 无磁盘 I/O、无网络、无依赖加载。
- 可在 hook 同步路径里反复调用（`session.py` 多处、client 每轮）。
- 比真实 tokenizer（tiktoken / Claude tokenizer，需加载词表 + BPE 合并）快几个数量级。

## 代价

- CJK 区段常用短语实际压缩比可能优于 1.5 token/char，会**高估 10-20%**。
- 预算方向的安全侧：提前触发 commit、少注入 context，而非超预算。
- 不感知 BPE 合并（如 `tokenization` 可能被切分成 2-3 个 BPE token 而非按字符算），但对预算门槛用途足够。

## 关键文件索引

- server 估算器: `openviking/utils/token_estimation.py`
- Message 层: `openviking/message/message.py:67`
- Session pending_tokens 重建: `openviking/session/session.py:450-466`
- client CJK-aware: `examples/claude-code-memory-plugin/scripts/lib/profile-inject.mjs:72`
- client chars/4: `examples/claude-code-memory-plugin/scripts/auto-recall.mjs:205`
- commit 触发读 server 数字: `examples/claude-code-memory-plugin/scripts/auto-capture.mjs:632,636`

## Related

- [context-management.md](./context-management.md) — 20000 阈值与 commit 触发机制
