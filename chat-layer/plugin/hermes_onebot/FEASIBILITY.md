# hermes_onebot 可行性结论（源码级）

> 结论一句话：**可行 —— 已实现并自证，零改动 Hermes 核心，默认 disabled，启用等主人拍板。**
> 本文是「能不能做、怎么做到、升级会不会丢」的结论文档；**怎么跑**见 `RUNBOOK.md`。

写于 2026-09-23。所有行号都是当天读源码时记的，路径以本机为准
（Hermes 装在 `/opt/hermes`，聊天门的 HERMES_HOME = `/opt/data/profiles/chat`）。

---

## 一、结论与三条硬证据

| 问题 | 结论 | 证据 |
|---|---|---|
| Hermes 能不能接 OneBot v11 小号？ | **能**，平台插件接口是公开的、不用改核心 | `ctx.register_platform()`（`hermes_cli/plugins.py:781-810`） |
| 会话会不会「每句新开」？ | **不会**，会话键由 SessionSource 现算、与消息无关 | `gateway/session.py::build_session_key`（`:654-695`） |
| 升级 Hermes 会不会丢？ | **不会**（插件在数据卷里、不是补丁） | `/opt/data` = 宿主 `/1000/mianmian/hermes`（btrfs 绑定挂载） |

自证：`tests/run_tests.sh` → `Ran 111 tests ... OK`；`doctor.py` 一条命令自查自检。

---

## 二、Hermes 平台插件接口（源码级）

1. **插件住在哪**：`$HERMES_HOME/plugins/<name>/`
   —— `plugins/plugin_loader.py:30-37` `user_plugins_dir() = get_hermes_home()/"plugins"`；
   `:40-` `iter_plugin_dirs()` 只认带 `__init__.py` 的子目录。
   聊天门跑的是 `hermes -p chat` → HERMES_HOME=`/opt/data/profiles/chat`
   → 插件目录是 `/opt/data/profiles/chat/plugins/`，**不是** `/opt/data/plugins/`。

2. **加载是需要显式开启的**：`hermes_cli/plugins.py:1311`
   `enabled = _get_enabled_plugins()  # None = opt-in default (nothing enabled)`，
   再 `:1320 to_load = {k: m for k, m in winners.items() if self._gate_manifest(m, disabled, enabled)}`。
   → **`config.yaml` 的 `plugins.enabled` 里没写名字，插件根本不会被 import。**
   实测：聊天门的 `plugins.enabled: []`，`gateway.log` 里 `onebot` 命中 **0 次**。

3. **平台适配器怎么注册**：插件模块暴露 `register(ctx)`，里面调
   `ctx.register_platform(name=…, label=…, adapter_factory=…, check_fn=…, is_connected=…, …)`
   （`hermes_cli/plugins.py:781-810`）→ 落进 `gateway/platform_registry.py:42 class PlatformEntry`
   （`source="plugin"`）。适配器基类 `gateway/platforms/base.py BasePlatformAdapter`，
   入站事件 `gateway/platforms/event.py MessageEvent`，出站回执 `SendResult`。
   `register_platform` 的 unknown kwargs 会 `TypeError` —— 所以本适配器用的
   `platform_hint` / `env_enablement_fn` / `allowed_users_env` 等键都是当期存在的。

4. **平台怎么算「启用」**：`config.yaml` → `platforms.<name>.enabled`，
   默认 **False**（`gateway/config.py:425 enabled=_coerce_bool(data.get("enabled"), False)`）；
   `get_connected_platforms()`（`:592-596`）要求 `c.enabled and _is_platform_connected(p,c)`。
   光有凭据/环境变量**不会**自动开平台（`:608-635` 的说明就是专门堵这个静默启用 bug 的，#31116）。
   本适配器给的 `is_connected`（`adapter.py:731-733`）只在 `enabled` 已为真时才被问到。

→ **两条闸门都要过才会真连**：`plugins.enabled` 里有 `onebot`（才轮到 `register` 被调用），
  且 `platforms.onebot.enabled: true`（才会真起反向 WS 监听）。

5. **零核心改动**：整个适配器只 import 公开模块
   （`gateway.config.Platform`、`gateway.platforms.base`、`gateway.platforms.event`、
   `gateway.platforms.helpers`、`gateway.platforms._shared.get_scoped_secret`），
   不改 `/opt/hermes` 下任何文件 → **Hermes 升级不冲突**（不像 AstrBot 那批补丁要重建后重打）。

---

## 三、会话 / umo 映射（「会话 id 一次定死」是怎么做的）

设计规矩（`CHAT-AGENT-DIRECTION.md` 四）：**会话 id 一次定死，禁止每句新开。**

实现是「**只从 QQ 号/群号推，不带任何消息级随机量**」：

| 方位 | 形状 | 在哪里定 |
|---|---|---|
| 聊天门（AstrBot 侧，现役） | `aiocqhttp:FriendMessage:<QQ>` | AstrBot `astr_message_event.py:106-108` |
| OneBot 适配器（Hermes 侧，本插件） | `onebot:dm:<QQ>` / `onebot:group:<群号>:<发送者>` | `adapter.py:525-533 _session_umo()` |
| Hermes 真正落库的会话键 | `<ns>:<platform>:<chat_type>[:<chat_id>][:<thread_id>][:<user>]` | `gateway/session.py::build_session_key`（`:654-695`），从 `SessionSource` 现算 |

关键点（**这就是「一次定死」**）：

- `_dispatch()`（`adapter.py:535-556`）每次都用**同一对 `(gid, uid)`** 现构 `SessionSource`：
  私聊 `chat_id=uid, chat_type="dm"`；群聊 `chat_id=gid, chat_type="group", user_id=uid`。
  只有 `message_id` 是消息级的，而 **`message_id` 不进会话键**（它只用于引用/回复）。
- 于是私聊键恒为 `<ns>:onebot:dm:<QQ>`、群聊键恒为 `<ns>:onebot:group:<群号>:<发送者>` ——
  连发 100 句、适配器重启、网关重启，键都不变 → 不会失忆。
- 群聊按「会话 + 发送者」分桶：`group_sessions_per_user` 默认 **True**
  （`gateway/run_adapters.py:1542-1546`）→ 同群里主人和群友各有独立上下文，不会串台。
- `_session_umo()` 那份字符串**不参与会话身份**，只用来做入站防抖分桶和日志辨识
  （`adapter.py:529-531` 里写明了），避免出现「两套会话 id 打架」。
- 出站方向：Hermes 的 `send(chat_id, …)` 不带 `chat_type`，所以适配器靠
  **入站学到的群号表**（`self.group_ids`）+ `metadata.chat_type` 判群/私
  （`adapter.py:560-565 _is_group_chat()`）。

**切换前必须做的迁移**（还没做，属启用前置）：AstrBot `data_v4.db` 里的对话历史要迁进 Hermes 会话，
否则切换当日她失忆。记忆三层不用迁（共用同一 Hindsight bank `mianmian-history`）。

---

## 四、出站分段 / 入站防抖（Hermes 本身没有，适配器内实现）

- **出站分段**：`segmentation.py` 纯函数、零依赖，参数逐条对齐 AstrBot 已验证实现
  （切分 `/AstrBot/astrbot/core/pipeline/result_decorate/stage.py:229-245`、
  阈值 `:220`）。`⁂`(U+2042) 分段，容错 `※`(U+203B) / `⸮`(U+2E2E)，符号不留进消息。
  **阈值 150 是上界**：整条超阈值就整段发、不切。
- **入站防抖**：`debounce.py` —— 10s 重置式窗口 / 45s 硬上限 / 默认仅私聊 /
  群聊按「会话+发送者」分桶（与 AstrBot 侧 `hermes_debounce` 同参数）。
- **平台提示词**：注册时带 `platform_hint`，告诉模型「要连发就在每句末尾写 `⁂`」
  （`adapter.py:767-772`）。

---

## 五、部署方式（两步，都不自动）

```
bash deploy.sh        # 把源码同步到 /opt/data/profiles/chat/plugins/onebot（幂等、不启用任何东西）
```
启用要主人拍板，改 `profiles/chat/config.yaml`：
```yaml
plugins:
  enabled: [onebot]          # 第一步：才轮到 register(ctx) 被调用
platforms:
  onebot:
    enabled: true            # 第二步：才真起反向 WS 监听
    extra: {read_only: false}   # 第三步（可选）：关掉只收不发
```
安全默认：`extra.read_only` 默认 **true** → 入站照收、出站一律拦并记日志
（`adapter.py:569-572`）。要真发必须显式关。

---

## 六、升级 / 重建会不会丢

**不会。** 三条理由：

1. **不是补丁**：不改 `/opt/hermes` 一个字节 —— Hermes 升版本、换镜像都不碰它。
2. **住在数据卷**：`/opt/data/profiles/chat/plugins/onebot` 落在 `/opt/data`，
   而 `/opt/data` 是宿主 `/1000/mianmian/hermes` 的 btrfs 绑定挂载（`findmnt` 实测）
   → 重建容器、换镜像都不动。
3. **自愈路径明确**：真出意外（比如手工删了、profile 换了）→ 在源码目录重跑
   `bash deploy.sh` 即可恢复；`doctor.py` 一条命令自检。

**仅有的两个真风险**（都在 RUNBOOK 的检查清单里）：
- Hermes 改了通道层插件 API（`register_platform` 的签名/`PlatformEntry` 的键）→
  `register` 会 `TypeError`。判据：`doctor.py` + `tests/run_tests.sh`，升级后各跑一次。
- 宿主上那份 profile 目录本身被换掉/删掉 → 重跑 `deploy.sh`。

对照：**AstrBot 侧那批补丁相反** —— 代码在容器内 `/AstrBot`（**不在卷里**），
升级/重建容器后会丢，必须照 `astrbot-patches.md` 重打（那条已经写进去了）。

---

## 七、没做 / 待主人拍板的

- **没启用**：`plugins.enabled` 仍为空、`platforms.onebot` 不存在 → 现役 NapCat↔AstrBot 链路零影响
  （实测 `gateway.log` 里 `onebot` 0 命中；网关仍是 2 个平台：api_server + a2a）。
- **没迁历史**：切换前要迁 AstrBot `data_v4.db` 的对话（见第三节末）。
- **没真连过 NapCat**：`tests/mock_onebot.py` 是本地 mock 协议端；
  真连要主人给 `ONEBOT_ACCESS_TOKEN` 并改 NapCat 的 WS 目标，属于启用动作。
