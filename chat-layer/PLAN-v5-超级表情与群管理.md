# OneBot 适配器：超级表情修复 + 两个缺口 + 群管理（2026-09-23）

> 性质：**已落地并上线**。作者：棉棉（干活门）。
> 相关：`PLAN-v4-group-gate.md`（唤醒档 gated）、`RESEARCH-可复用插件-qzone与群管理.md`、`RESEARCH-qzone-底层开源库.md`。

---

## 一、超级表情看不见 —— 根因与修复

**症状**：主人在群里发 QQ「超级表情/大表情」，聊天门那头完全看不到。

**根因（代码级）**：`onebot_proto.py` 里有**两条独立的 `if/elif` 解析链**
（`extract_text` 给 LLM、`extract_window_text` 给群窗口），**两条都没有 `else` 兜底**：

- 全仓 `marketface`（超级表情的段类型）**零命中** → 落进"没有分支匹配"的空洞
- `face`/`mface` 在 LLM 那条链里是 `continue`，**直接扔**
- 未知段既不计数也不打日志 → **静默消失**

连锁后果：私聊只发一个表情 → `extract_text()` 返回空 → `should_ignore` 判 `no_text` → **整条消息被丢弃**，她连"对方发了个表情"都不知道。群聊里连窗口都进不去。

**修复**（`onebot_proto.py`）：

1. **收敛成一张表 + 一个实现**：`MEDIA_PLACEHOLDER` 字典 + `_render()`，两条链共用 → 结构上杜绝"改了一边忘了另一边"
2. 补齐 `marketface`，另加 `bface/dice/rps/shake/poke/music/json/xml/markdown/forward/node`
3. `_media_placeholder()` **保证永不返回空串**；认不出的段落 `[未支持的消息段:x]`
4. `extract_window_text` 现在直接委托 `_render`（两条链已无差异）

**行为变更（故意）**：私聊纯表情消息不再被丢弃，会带 `[表情]`/`[超级表情]` 到她面前。
4 条钉旧行为的用例已同步改写，每条注明原因（见 `tests/check_group_window.py`）。

**未查实**：没抓到过一条真实的超级表情入站事件，所以 NapCat 上报时 `sub_type` 的确切取值没验过。文档说「以 image 消息段上报(子类型区分)」。

---

## 二、两个真实缺口

### 缺口 1：通知/请求事件全被丢

`should_ignore()` 第一句 `post_type != "message"` 就整条丢 → **入群通知、撤回、禁言、加群申请一个都进不来**。这是入群欢迎/防撤回/入群审批的硬前置。

**修复**：新增 `classify_event()` 做路由分类；适配器在 `_ingest` **最前面**分流，`notice`/`request` 走群管理链路，不再经过 `should_ignore`。

### 缺口 2：出站引用回复没实现

`send(reply_to=...)` 这个形参**定义了但从头到尾没人用**；入站能解析引用、出站不会发，方向不对称。

**修复**：`build_action()` 支持 `reply_to` → 在消息数组前插 `reply` 段；**只挂在第一条**（分段发送时后面几条不会跟着变成引用）。

---

## 三、群管理（`group_admin.py`，新增）

**架构原则**：模块**纯逻辑零 I/O**（只依赖标准库 + `onebot_proto`），输入事件 → 输出 `Intent` 列表；真正的执行由适配器 `_execute_intents()` 做。好处：可单测、无副作用、执行层可审计。

能力：入群欢迎 / 退群提示 / 加群审批（manual·auto_approve·auto_reject）/ 关键词回复（exact·contains·regex）/ 违禁词（撤回+可选禁言）/ 防撤回 / 管理命令（ban·unban·kick·card·recall·essence·stats）/ 发言统计。

权限：主人名单 + 白名单群**双条件**；非主人一律不产出执行类 Intent。

### 接线时抓到的两个坑

1. **`ACTION_INTERNAL`（`internal_noop`）不是真 action。** 那类 Intent 只带"给她看一眼"的说明（如"有人撤回了但缓存没原文"）。我第一版把**所有** Intent 都当 action 发帧 → 协议端会回「未知 action」。现在在 `_execute_intents` 里分流：只记笔记 + 日志，不发帧。
2. **`{at}` 生成的是裸 CQ 码文本。** 数组格式里文本段内的 CQ 码各协议端表现不一致 —— NapCat 文档写的"支持使用 CQ 码发送"是针对**字符串格式**的。新增 `cq_to_segments()`，统一转成真的 `at`/`image`/`face` 段。**不赌协议端会解析。**

### 安全设计

- 适配器侧**默认关**（`group_admin_enabled: false`）；模块缺失也一律当关 → 无感降级
- 模块内「动手」的能力默认全关：入群欢迎关、违禁词关、防撤回关、加群审批走 `manual`（只通知）
- 每个 Intent 独立 try，**一个失败不拖垮其余**

### 可观测性

`onebot-state.json` 新增：`group_admin_enabled` / `group_admin_active` / `group_admin_actions` / `group_admin_notes`。
**`enabled=true` 但 `active=false` = 开关开了模块却没加载**，这两个字段就是用来分清的。

---

## 四、验证

```
bash tests/run_tests.sh  →  Ran 341 tests … OK (skipped=3)
```

新增用例覆盖：
- 超级表情两个口径都可见；未知段不静默丢；私聊表情不再被吞
- 出站 `reply_to` 真的产出 reply 段；**只挂第一条**；不带时不产出
- 群管理：默认关不动手；notice 不再被吞；`{at}` 落成真 `at` 段；`inner_noop` 不发帧
- **action 名白名单守卫**：`group_admin.py` 产出的 action 必须落在**本机对 NapCat 4.18.28 实测存在**的那一批里 —— 防"照文档写了个不存在的 action 名"

上线核验：`gateway-chat` up（pid 125901）、`onebot connected`、
state 里 `group_admin_enabled: true` + `group_admin_active: true` + `group_wake_mode: gated`。

---

## 五、当前配置（`config.yaml`）

| 能力 | 状态 |
|---|---|
| 入群欢迎 | **开**（`{at} 欢迎进群～`） |
| 防撤回 | **开**（把撤回内容复述一遍） |
| 管理命令 | **开**（`/` 前缀，仅主人） |
| 发言统计 | **开**（内存，重启清零） |
| 加群审批 | `manual` —— 只私聊通知主人，**不自动批** |
| 关键词回复 | 空（不回话，等着填） |
| 违禁词 | **关** |
| 退群提示 | **关** |

回退：`cp config.yaml.bak-before-groupadmin-<ts> config.yaml` + 重启 `gateway-chat`。
代码层不用退（全是增量，旧行为逐字保留）。

---

## 六、还没做 / 已知短板

- **`[SILENT]` 依赖模型听话** —— SOUL 里写了规矩，但模型在群场景的真实服从率没实测过
- **管理命令的 action 名只对白名单做过静态校验**，没真发过（真发会在群里真禁言真人）
- **群管理笔记（`_group_admin_notes`）只进日志，没自动注入 LLM 上下文** —— 她目前不会主动说"有人撤回了消息"
- **违禁词/自动批群默认关** —— 要开得自己改配置，这是故意的
- 防撤回会把别人撤回的内容复述出来，**社交上有风险**，不想要就关掉
