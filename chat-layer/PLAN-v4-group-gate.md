# PLAN v4 — 群聊「让她自己考虑」（gated 档）

> 性质：**已落地并上线**（不是提案）。时间 2026-09-23。作者：棉棉（干活门）。
> 上位文档：`PLAN-group-chat.md`（B/C1 阶段）、`RESEARCH-group-chat-two-benchmarks-mapping.md`（麦麦 + qq-bridge 双基准调研）。
> 一句话：**免费规则闸决定「叫不叫模型」，模型决定「开不开口」。**

---

## 0. 为什么改、跟旧路线什么关系

主人的原话是「干脆就用麦麦那种让他自己考虑」。但这句话里藏着一个必须说破的矛盾：

| 做法 | 成本 | 人味 |
|---|---|---|
| 全交给规则（麦麦 `reply_necessity`） | **0**（不命中根本不叫模型） | 死鱼 —— 闸放过去的话它必须答 |
| 全交给模型（每条群消息都进 LLM） | 每条群消息都付钱 | 最好 |
| **两段式（本方案）** | 只有过闸的付钱 | 过闸的由她自己定回不回 |

**关键：`[SILENT]` 不省钱。** 模型既然已经被叫醒，那一轮 token 就已经花了 —— 它只能省"噪音"，不能省"钱"。**省钱的地方只有免费闸。** 上一版 `Auroic/decider-0.6B` 路由器之所以被砍，就是因为它是"用花钱的东西去省花钱的东西"：0.6B 判断力实测 3–6/8、跨运行翻结论，而它省下的那点钱还不够养它自己。

结论：**砍掉小模型路由器，改在适配器层加一道免费闸 + 给模型一个闭嘴开关。**

---

## 1. 设计

```
群消息 ──► ① 采集窗口（照旧，0 LLM、0 记忆写入）
        └► ② 免费闸 gate_score()   ← 本方案新增，纯函数、纯内存
              ├─ 不过闸 ──► 到此为止（0 token）         ← 省钱在这
              └─ 过闸   ──► ③ 唤醒模型
                              ├─ 回 [SILENT] ──► 群里一个字不发  ← 人味在这
                              └─ 正常回     ──► 发
```

### 免费闸打分（照麦麦 `reply_necessity` / qq-bridge wake 概率重写）

加项：疑问/求助信号词 `+0.35`；可见长度 ≥6 `+0.20`；≥15 再加 `+0.10`；闲置压力 `+0~0.25`（她越久没说话越愿意接）。
减项：纯表情/纯符号 `−0.40`；可见长度 ≤2 `−0.30`；与近 12 条复读/刷屏 `−0.25×相似度`；自说率超 50% 后 `−0.6×(占比−0.5)`。
阈值 `group_gate_threshold`，默认 `0.35`。**@ / 被回复 / 被点名 在 gated 下依然直通，闸管不到它们。**

纯函数、纯内存、不读文件、不连网 —— 有源码级守卫盯着（见 §4）。

---

## 2. 落点（改了哪些文件）

| 文件 | 改动 |
|---|---|
| `plugins/onebot/group_wake.py` | 新增 `MODE_GATED`、`GateState`、`gate_score()`、`silence_of()`、`GATE_DEFAULT_THRESHOLD`、`SILENCE_MARKER`；`decide()` 加 `gate_state/gate_threshold/now` 三个 kwarg + `gate` 分支；`TRIGGER_LABEL` 加 `gate` |
| `plugins/onebot/adapter.py` | 读 `group_gate_threshold`；`_gate_state(gid)` 按群维护；`_collect_group()` 里每条群消息喂 `note_incoming`；`_group_wake_trigger()` 传状态、记 `_gate_wake_count`；`send()` 拦 `[SILENT]`（放在连通性检查**之前**，因为那是决定不是投递）；发送成功后 `note_self_spoke()` |
| `SOUL.md`（聊天门） | 群聊节加三条：被叫醒可以不开口 → 只回 `[SILENT]`；@/点名/被回复**不适用**；有正常内容时不许带它 |
| `config.yaml` | `group_wake_mode: mention-only → gated`；新增 `group_gate_threshold: 0.35`（备份 `config.yaml.bak-before-gated-<ts>`） |
| `tests/check_group_wake.py` | 新增 `TestGatedMode` 15 例 |
| `tests/check_adapter_e2e.py` | 新增 `TestGroupGatedWake` 7 例（走真适配器 + mock 协议端） |

**没动的**：`mention-only` / `full` / `collect-only` 三条既有语义逐字保留；记忆隔离硬前置、每分钟/每小时成本闸、分段、防抖全部照旧。

---

## 3. 验证（全部实测，非推断）

```
check_group_wake.py    39 tests OK   (原 24 + 新 15)
check_adapter_e2e.py   59 tests OK   (原 52 + 新 7)
check_group_window.py  23 tests OK
check_debounce.py      29 tests OK
check_health.py        26 tests OK
─────────────────────────────────────
合计                  176 tests OK
```

端到端关键三例（`TestGroupGatedWake`）：
- 连发 `哈哈/嗯/😀😀😀/666` → `dispatched == []`、`_group_llm_calls == 0`、但 4 条都进了采集窗口 → **省钱是真的没花钱，不是"模型说它不想回"**
- 发 `这个报错怎么处理啊？` → 唤醒 1 轮，`_gate_wake_count == 1`
- `send("55555", "[SILENT]")` → mock 协议端 `sent_texts("send_group_msg") == []`、`message_id is None` → **群里真的一个字都没出去**
- 同一句 `[SILENT]` 走私聊 → **照发**（不能拿它挡主人）

上线核验：`gateway-chat` up（pid 111254）、`onebot connected`、`onebot-state.json` 里 `group_wake_mode: gated`、`group_memory_isolated: true`、`group_llm_calls: 0`。

---

## 4. 两条红线 + 各自的机器判据

1. **门控层绝不许碰 LLM / 记忆库 / 网络。**
   判据：`check_group_wake.py::TestGatedMode::test_gate_layer_never_touches_llm_or_memory` —— 用 `ast` 只扫门控段的**代码**（`Name`/`Attribute`/`Import`），跟黑名单取交集，非空即红。注释和文档串不参与（否则"不碰 LLM"这句说明文字自己就把守卫弄红）。
2. **群消息绝不进主记忆库。**
   沿用既有 `check_group_window.py::TestNoMemorySink`，未改动。

---

## 5. 调参入口（就一个）

`config.yaml` → `onebot.extra.group_gate_threshold`

| 值 | 效果 |
|---|---|
| `0.0` | 每条群消息都叫模型 ＝ 纯烧钱（等于 `full`） |
| `0.35` | **当前值**，闲聊不叫、有问题才叫 |
| `0.5` | 更闷，只有明确疑问/求助才醒 |
| `1.0` | 几乎只剩 @/点名/被回复（≈ `mention-only`） |

**观察指标**（`onebot-state.json`）：`_silence_count` 高 → 闸太松（叫醒了又不想说），调高阈值；`group_llm_calls` 涨太快 → 同理；`group_wake_count` 长期为 0 → 闸太紧，调低。

---

## 6. 回退（一行）

```bash
# 退回"只有被@/点名/被回复才答"：
sed -i 's/^      group_wake_mode: gated$/      group_wake_mode: mention-only/' \
  /opt/data/profiles/chat/config.yaml
/command/s6-svc -r /run/service/gateway-chat
```
代码层无需回退：新档位是**增量**，旧三档语义没动。config 备份在 `config.yaml.bak-before-gated-<ts>`。

---

## 7. 还没做 / 已知短板

- **成本闸是全局的**（`group_at_max_per_minute: 2` / `per_hour: 30`），没有按群分账。多群时会互相挤。人少无所谓，群多了要加。
- **`GateState` 在内存里**，进程重启清零 → 重启后第一次闲置压力按满额算（偏爱说一点点），可接受。
- **闸的权重是拍的**（照麦麦的结构，数值自定）。**没有拿真实群消息校准过** —— 我试着用现成的群窗口量了一遍（`onebot-groups/<GROUP_ID>.jsonl`），**窗口里只有 3 条真实群消息**，量出来的"过闸率 67%"统计上没有意义，别当真数。**阈值 0.35 目前仍是猜的。** 要校准只有一条路：让它先跑几天，攒够几百条真实群消息，再拿我们自己的标注回测 —— 不能用别人的 86.4%（那是 22 条样本的作者自测）。
- **`[SILENT]` 依赖模型听话**。SOUL 里写清楚了，但没实测过模型在群场景的真实服从率。上线后看 `_silence_count` 是否随日志里的唤醒次数同步上涨即可判断。
