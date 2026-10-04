# AstrBot 源码补丁登记（重建/升级后必须重打）

AstrBot 的代码在容器内 `/AstrBot/`，**不是**我们挂载的卷。所以下面这些补丁在 **AstrBot 升级或重建容器后会丢** ✗ —— 重建后照着这份文件重打，否则会静默回到有病状态。

> ✅ **2026-09-23 起已自动化，不用再手打**：补丁载荷 + 自愈入口在 `chat-layer/patches/`
> （说明见 `chat-layer/patches/README.md`），compose 里 astrbot 的 `entrypoint` 指向它，
> **重建容器时 python 起之前自动幂等重打**。人工核验入口：
>
> ```bash
> # 宿主：模拟全新容器重建（期望尾行 [VERIFY] 补丁已生效 ✓）
> docker run --rm -v /vol1/1000/<USER> \
>   -v /tmp/astrbot-sim-data:/AstrBot/data -e PATCH_ONLY=1 \
>   --entrypoint /bin/bash soulter/astrbot:latest /opt/astrbot-patches/astrbot-entrypoint.sh
> # 线上容器：机器判定自检（exit 0 = 5 条判据全过）
> docker exec astrbot python3 /opt/astrbot-patches/verify_patch.py
> # 一键补打（容器不是 compose 建的时兜底）
> bash /vol1/1000/<USER>
> ```
>
> ⚠️ `patch_astrbot_context.py` 是**合并版**：一次覆盖下面补丁 1 + 补丁 2 的全部 5 处锚点。
> 单独跑任一历史分步脚本（`tmp/patch_astrbot_context_v2.py` / `..._v3.py`）都会**只修一半 → 仍然坏**。

配套重建检查表：`/opt/data/ops-changelog/README.md`（运维总入口）。

---

## 补丁 1：工具参数与形参重名（2026-09-23）

**症状**：猫猫调 Hindsight 的 `retain` 直接崩，回显
`astrbot.core.astr_agent_tool_exec.call_local_llm_tool() got multiple values for keyword argument 'context'`；`sync_retain` 则是 120 秒超时。

**根因**：Hindsight 的 MCP 工具 `retain`/`sync_retain` 自带一个叫 `context` 的参数（参数表：`content, context, timestamp, tags, metadata, document_id, strategy[, update_mode]`），而 AstrBot 的 `call_local_llm_tool` 第一个形参也叫 `context` → 撞名 → TypeError 直接退出。**与模型快慢无关**。

**改法（必须两处一起改，少一处就坏）**：

1. 函数侧 `/AstrBot/astrbot/core/astr_agent_tool_exec.py:743` — 给第一个形参加 PEP 570 的 `/`（仅位置参数）：

```python
async def call_local_llm_tool(
    context: ContextWrapper[AstrAgentContext],
    /,                                   # ← 新增这一行
    handler: T.Callable[
        ...
    ],
    method_name: str,
    *args,
    **kwargs,
) -> T.AsyncGenerator[T.Any, None]:
```

2. 调用侧同文件 `:683`（全库唯一调用点）— 把关键字传参改成位置传参：

```python
        wrapper = call_local_llm_tool(
            run_context,                 # ← 原来是 context=run_context
            handler=awaitable,
            method_name=method_name,
            **tool_args,                 # ← 工具的 context 参数从这里进，现在能正常落进 kwargs
        )
```

> ⚠️ **只改第 1 处会全线崩**：本地工具会全部报 `missing argument: 'context'`（因为唯一调用点是 `context=` 关键字传的，加了 `/` 就不许关键字了）。第一次就是这么翻车的，改完立刻回滚。

**安全性依据 + 验证方式（必须机器判定，不许靠眼看）**：
- `inspect.signature(call_local_llm_tool).bind(ctx, handler=f, method_name="m", context="工具参数", content="x")` **绑定成功**即通过 —— 这条能同时证明「重名不再冲突」和「调用路径不缺参」。实测输出：`绑定成功 ✓ 参数落在 ['context','handler','kwargs','method_name']`
- `py_compile` 通过、AstrBot 正常启动、两个 provider 正常加载

**脚本**：合并进 `/opt/data/chat-layer/patches/patch_astrbot_context.py`（幂等，两处都会检查后跳过；自动备份到 `/AstrBot/data/_setup_backup_<ts>/`）。**重建后由 compose entrypoint 自动重打**；单独手工补打用 `patches/apply-from-host.sh`。

**验证方式**：让小号调一次本地工具（定闹钟/记一条），不报 `multiple values` 也不报 `missing argument` 即通过。

---

## 补丁 2：工具自身参数叫 `context` 时仍撞形参（2026-09-23，同一 bug 的第二层）

**症状**：补丁 1 打完后，`retain` 仍报
`Exception: Tool handler parameter mismatch, please check the handler definition. Handler parameters: kwargs: Any`，
其内层真因是 `TypeError: _PermissionGuardedTool.call() got multiple values for argument 'context'`。

**根因（两层，缺一层就还炸）**：
1. `get_full_tool_set()` 把**每个**工具都包一层 `_PermissionGuardedTool`（`func_tool_manager.py:246` 起，`async def call(self, context, **kwargs)`）→ 执行时走 `_execute_local` 的 `call` 分支，即 `handler(context, **kwargs)`；
2. Hindsight 的 MCP 工具 `retain` 参数表里**自带 `context`**（`content, context, timestamp, tags, metadata, document_id, strategy, update_mode`）→ 运行时上下文 + 工具参数同名 → Python 判重复赋值。
3. 底层 `MCPTool.call(self, context, **kwargs)`（`agent/mcp_client.py:814`）是同一个坑的第二层，`_PermissionGuardedTool` 会再调它。
4. `call_local_llm_tool` 的 `except TypeError` 把真错吞掉、换成「parameter mismatch」文案（`handler_param_str` 又是 `params[1:]`，于是只剩 `kwargs: Any`）→ 报错文本与真因完全脱节。

**改法（3 个文件，两处首参加 PEP 570 `/`，外加让报错带上真因）**：

1. `astrbot/core/provider/func_tool_manager.py:246`
```python
    async def call(self, context: Any, /, **kwargs: Any) -> Any:
```
2. `astrbot/core/agent/mcp_client.py:815`
```python
    async def call(
        self, context: ContextWrapper[TContext], /, **kwargs
    ) -> mcp.types.CallToolResult:
```
3. `astrbot/core/astr_agent_tool_exec.py:800`（只改文案，便于以后一眼看到真因）
```python
            f"Tool handler parameter mismatch, please check the handler definition. "
            f"Original error: {e}. Handler parameters: {handler_param_str}"
```

> 安全性：改前已核全库 `call(` 调用点，这两个 `call` 只有两处调用、**都是位置传参**（`astr_agent_tool_exec.py:737` 的 `tool.call(run_context, **tool_args)`、`func_tool_manager.py:269` 的 `self._wrapped.call(context, **kwargs)`），加 `/` 不影响任何调用方。

**脚本**：合并进 `/opt/data/chat-layer/patches/patch_astrbot_context.py`（幂等、带前置断言、`py_compile` 自检、自动备份到 `/AstrBot/data/_setup_backup_<ts>/`；历史单步脚本已降为 `patches/legacy-patch_astrbot_context_v3.py` 留档，**别再单独跑**）。
**自检（重建后自动跑）**：`/opt/data/chat-layer/patches/verify_patch.py`（容器内 `/opt/astrbot-patches/verify_patch.py`）——AST 判 5 条，exit 0 = 生效。
**验证工具**：`/AstrBot/data/mianmian-tmp/verify_tools.py`（容器内 scratch 进程跑真实执行链路：`FunctionToolExecutor.execute` → `_execute_local` → `call_local_llm_tool` → `_PermissionGuardedTool.call` → 真 MCP / 真内置工具）。⚠️ 只跑 `local` / `file` 模式，**别跑 `retain`**（会往 Hindsight 写测试记忆）。
**验收实测（2026-09-23 04:32）**：`retain` 返回 `{"status":"accepted", operation_id:"ea4519f5-…"}`；`astrbot_file_read_tool` 读文本返回正文、读 PNG 返回 `ImageContent` base64；handler 型本地工具实收 `['content','context','path']`（`context` 值原样落进 kwargs）。

---

## 补丁持久化（2026-09-23）：重建后自动重打，不再靠人记

**为什么要做**：补丁只在容器可写层里，`docker compose up -d --force-recreate`、换镜像、一手搓的
`docker run` 都会让它消失，且**表现是静默退化**（本地工具报错，不会主动喊人）。

**做法（三道防线，详见 `chat-layer/patches/README.md`）**：

| 防线 | 位置 | 说明 |
|---|---|---|
| 自动（主） | `chat-layer/docker-compose.yml` + `chat-layer/patches/` | `entrypoint: ["/bin/bash","/opt/astrbot-patches/astrbot-entrypoint.sh"]` + 只读挂载 `./patches:/opt/astrbot-patches:ro`。容器起来时先幂等重打 + 自检，再 `exec` 镜像原 CMD（`python main.py`） |
| 一键（兜底） | `chat-layer/patches/apply-from-host.sh` | 容器不是 compose 建的时用：送补丁进容器 → 打 → 自检 → 可选重启 |
| 留痕 | 容器内 `/AstrBot/data/patches-boot.log`（在卷里） | 每次启动记录是「已改」还是「跳过」、自检过没过 |

**改动文件**：`chat-layer/patches/{patch_astrbot_context.py, verify_patch.py, astrbot-entrypoint.sh,
apply-from-host.sh, README.md}`；`chat-layer/docker-compose.yml`（astrbot 段 +8 行）。
compose 备份：`chat-layer/backup/docker-compose.yml.bak-before-patch-persist-20260922-235439`
（md5 `4dceadef6e7f09d2ebd64f3a4fc13a85`）。

**"模拟重建"验收实测（2026-09-23 07:54，宿主）**：

```
docker run --rm -v <patches>:/opt/astrbot-patches:ro -v /tmp/astrbot-sim-data:/AstrBot/data \
  -e PATCH_ONLY=1 --entrypoint /bin/bash soulter/astrbot:latest /opt/astrbot-patches/astrbot-entrypoint.sh
→ 已写入并通过 py_compile：astr_agent_tool_exec.py / func_tool_manager.py / mcp_client.py
→ 改动 5 处 / 跳过 0 处｜PATCH_OK
→ [VERIFY] 补丁已生效 ✓（3 处 context 仅位置参数 + 调用点位置传参 + 报错文案带真因）

# 同容器连打两次 → 第二次「改动 0 处 / 跳过 5 处」（幂等 ✓）
# 同一入口 + exec 原 CMD + 真 import 判定：
#   _PermissionGuardedTool.call: first=self kind=POSITIONAL_ONLY
#   MCPTool.call: first=self kind=POSITIONAL_ONLY
#   call_local_llm_tool: first=context kind=POSITIONAL_ONLY
#   bind(context=工具参数) 成功 ✓ 参数 ['context','handler','kwargs','method_name']  → SIM_RESULT: PASS
```

**线上容器现状核验**：`bash chat-layer/patches/apply-from-host.sh astrbot --no-restart` →
「改动 0 处 / 跳过 5 处」+ `[VERIFY] 补丁已生效 ✓` + 三个函数签名自检 ✓
（说明线上容器此刻就是补丁态；compose 的 entrypoint 自愈会在下次重建时接管）。

**真·线上重建实测（2026-09-23 08:08，不是模拟）**：在宿主跑
`cd chat-layer && docker compose up -d --force-recreate astrbot`（重建前容器是 09-20 建的、
`Entrypoint=null`、**没有** patches 挂载 → 自动自愈从未在线上生效过，本次才是第一次真跑）：

```
容器重建 → entrypoint 自动执行 → patches-boot.log：
  已写入并通过 py_compile：astr_agent_tool_exec.py / func_tool_manager.py / mcp_client.py
  改动 5 处 / 跳过 0 处｜PATCH_OK
  [VERIFY] 补丁已生效 ✓（3 处 context 仅位置参数 + 调用点位置传参 + 报错文案带真因）
重建后 docker inspect：Entrypoint=["/bin/bash","/opt/astrbot-patches/astrbot-entrypoint.sh"]
  Mounts 含 chat-layer/patches → /opt/astrbot-patches
docker exec astrbot python3 /opt/astrbot-patches/verify_patch.py → exit 0
# 同容器 docker restart（可写层保留）→ 「改动 0 处 / 跳过 5 处」→ 幂等 ✓
```

**⚠️ 重建的另一半代价（本轮实测踩到，登记备查）**：AstrBot **插件自己的 pip 依赖装的是容器可写层**
（`pip_installer.install()` 只在 `is_packaged_desktop_runtime()` 为真时才写持久卷
`/AstrBot/data/site-packages`，Docker 里该函数恒为假，源码 `utils/runtime_env.py:9`）→
**重建后 AstrBot 会在启动时串行重装插件依赖，期间整个聊天门不可用**（平台适配器还没起）。
2026-09-23 08:08 重建后 `astrbot_plugin_qzone` 的 `pillowmd`(20.2MB) 下载耗时 3m50s
（~107 kB/s）、接着 `fonttools` 卡死 10 分钟无流量（`docker stats` net=0）→ 聊天门一直 down。
处置：把该插件移出加载路径（`stack/astrbot/data/plugins_disabled/astrbot_plugin_qzone`，**可 mv 回**）
后 `docker restart astrbot` → 08:14:16 平台适配器恢复、插件正常加载。

> 结论：**补丁会自愈，插件依赖不会**。重建前先想清楚有哪些插件带 `requirements.txt`，
> 或者接受开机几分钟的依赖重装窗口。彻底修法（未采用，风险已评估）：给 astrbot 加
> `ASTRBOT_DESKTOP_CLIENT=1` + `ASTRBOT_ROOT=/AstrBot`（`get_astrbot_root()` 会在 desktop 模式返回
> `~/.astrbot`，必须同时设 ASTRBOT_ROOT 才不把 data 路径带飞）→ 依赖落到持久卷 `data/site-packages`。
> 涉及运行模式语义，**留给主人拍板**。


**真链路复测（不写记忆，2026-09-23 07:5x）**：容器内 `verify_tools.py local` → handler 型本地工具实收
`['content','context','path']`、`context='工具参数里的 context'`；`verify_tools.py file` → 读文本/
读图正常返回。两处都不再报 `multiple values` / `missing argument`。

---

## 测试残留清理（2026-09-23 收尾）

上次验证往 Hindsight 写了两条测试记忆（文档 `8da1f204-…` / `6a18a2de-…`，tag `tool-fix-verify`），
已删；本次全库复核：

```bash
# 1) 全量 memory unit 扫描（29322 条）+ 文档全量列表（1823 篇）搜测试关键词
#    关键词: tool-fix-verify / 验证文本 / verify_tools / 工具参数里的 / astrbot_file_read_tool / 8da1f204 / 6a18a2de
# → 文档命中 0；memory unit 命中 2 条，其中 1 条是误报（"验证文本长度"），另 1 条是过期状态快照：
#    e60269c7「两条测试记忆留库待清理」→ 已 PATCH state=invalidated（软退役，可 revert），
#    废弃后从 /memories/list 消失（属预期，别当"被删了"）
curl -s -H 'Accept-Encoding: identity' "http://172.17.0.1:8888/v1/default/banks/mianmian-history/documents?limit=500&offset=0"
```

**未清掉的一类（如实说明）**：那两条测试 retain 的 **operation 记录** 还在
（`1a46cb36`/`69e0b237`/`817400c6`/`ea4519f5`，均 completed）。Hindsight 的
`DELETE /operations/{id}` **只能 cancel `pending`**（对 completed 一律 409），公开 API 删不掉；
删它们要直连 pg `delete from async_operations`，属不可逆 DB 写操作 —— 未获主人直接授权，**没做**。
这 4 条是纯任务日志（不含记忆内容，其产物文档已删），无功能影响。

### 2026-09-23 08:1x 二次复核（机器判定，可重跑）

脚本：`scripts/verify_toolfix_residue.py`（文档全量 + 关键词服务端检索 + 语义 recall 探针，可重复跑）
实测输出：

```
文档全量：1837 篇
  已删文档 8da1f204-…: 不存在 ✓     已删文档 6a18a2de-…: 不存在 ✓
  [DOC ] 0 命中
memory unit（关键词 tool-fix-verify / verify_tools / astrbot_file_read_tool / 测试记忆 / 待清理
             × state=valid,invalidated）：命中 3 条，全部 state=invalidated（已退役，不参与判定）
  b498ce8d「库中留2条测试记忆，tag=tool-fix-verify，未清，等主人发话」→ 本轮退役
  1ed6fc27「库中残留 2 条测试记忆待清理」→ 本轮退役
  e60269c7「两条测试记忆留库待清理」→ 上一轮退役
  另有 9 条含关键词的会话记忆经人工复核为正常记录/正确结论，已列入脚本 REVIEWED_OK
结论：无残留 ✓（有效记忆 0 命中；已退役条目不参与判定）… exit 0
```

复跑命令：`python3 /opt/data/scripts/verify_toolfix_residue.py`（exit 0 = 干净）；
只看命中清单用 `python3 /opt/data/scripts/residue_hits.py`。

**两条被退役的是「过期状态快照」而非测试内容**——它们本身是会话记忆（document
`20260922_234245_a2969e50`），内容说「那 2 条测试记忆还在库、待清理」，而事实是已删除 →
留着会持续误导召回（依据 `ops-changelog/README.md` §7 坑 16：写下错误结论后必须标废弃）。
今天再次读回召回：两条已不出现在结果里（早期那批 recall 的 12 条里没有它们）。


---

## 附二：hermes_report（聊天门侧「主动回执」接收端，聊天门那侧的猫猫自己写的）

**位置**：`/AstrBot/data/plugins/hermes_report/`（`main.py` 11,974 字节，owner uid 1003；token 在 `.report_token`，**不得回显/外传**）

**作用**：干活门跑完长任务 → `POST /report`（带 `X-Report-Token`）→ 插件立刻把结果发到主人私聊 + 写一条 Hindsight 记忆（tag `worker-report`）。

**关键改动（2026-09-23，我改的）**：默认 `listen_host` 从 `127.0.0.1` 改成 **`172.17.0.1`**
- 原因：AstrBot 是 host 网络，绑 `127.0.0.1` 时**只有宿主自己能访问**，干活门（Hermes 容器，docker 桥接网络）够不着 ✗
- 绑 `172.17.0.1` = 只有容器可达、**不暴露到局域网**，且有 token 鉴权
- 备份：`/AstrBot/data/_setup_backup_hermes_report_main.py`
- 实测证据（改后）：宿主 `curl http://172.17.0.1:8098/health` → `{"ok": true, ...}`；**从 Hermes 容器直连同样 200** ✓；真实 POST 一条回执 → `{"ok": true, "sent": true, "umo": "aiocqhttp:FriendMessage:<OWNER_QQ>"}` ✓ 主人私聊收到 ✓

**已知缺口**：回执只到主人私聊 + Hindsight，**不进她自己的会话上下文** ✗ → 她下一轮仍不知道任务完成了。补法待定（把回执注入会话历史 / 或让她每轮 recall `worker-report`）。

**接口**：`POST /report` body `{task_id, status(ok|fail|partial), title, summary(≤400字), path?, source?}`；`GET /health`。端口默认 8098。

---

## 附：AstrBot 侧非补丁类配置（重建后要恢复，别只恢复补丁）

| 项 | 值 | 说明 |
|---|---|---|
| provider | `deepseek/deepseek-flash`（默认聊天）+ `local/bonsai2-27b`（看图） | 本地端点 `http://172.17.0.1:8081/v1`，`timeout: 900`、`max_context_tokens: 16384` |
| `default_image_caption_provider_id` | `local/bonsai2-27b` | 图片交给本地 27B 描述；图不出门、不花钱（实测 HTTP 200，描述准确，但会被记忆抽取队列拖慢） |
| `platform_settings.segmented_reply` | regex `.*?[。？！~…※]+|.+$`、cleanup `[※]` | 模型每条短消息末尾写 `※` 当分隔符，`※` 不进消息 |
| persona `mianmian` | `tools`/`skills` 必须为 **NULL**（不能是 `[]`） | `[]` = 一个工具都不给，会表现为"工具够不着" |
| MCP | `http://172.17.0.1:8888/mcp/mianmian-history` | **必须带 bank 路径**，否则落到空库 `default`（recall 0 条、node=0） |
| 插件 | `hermes_delegate`（派活 A2A 9901）、`hermes_memory`（**记忆桥 v1.1.0：每轮自动 retain 回填 + 每轮请求前按当前消息 recall 召回并注入 `req.contexts`**，async 写入不变，见 `plugin/hermes_memory/README.md`）、`hermes_report`（长任务主动回报接收端，**已改绑 172.17.0.1:8098**，见 `proactive-report.md`）、`hermes_debounce`（连发合并：`wait=10s` 重置式窗口 / `max_wait=45s` / `scope=private`） | 源码副本在 `/opt/data/chat-layer/plugin/`（**改这里，再同步到 `/opt/data/stack/astrbot/data/plugins/<插件名>/`，两侧 md5 必须一致**；`hermes_forward`/`hermes_report` 是 compose 直接挂载，其余在 data 挂载里）；配置分别见 `data/config/<插件名>_config.json`。**`hermes_memory` 的 recall 只对主人私聊 umo 注入**（`recall_umos`），失败/超时降级为不注入；**改完需 AstrBot 侧生效**：容器有 `ASTRBOT_RELOAD=1`（compose 已加）时自动热重载，否则要重启容器。⚠️ **已知且主人接受**：10 秒窗口的缓冲只在内存（未落盘），AstrBot 重启恰卡在窗口内时那 1~2 条会静默丢弃 —— 设计取舍，别当 bug 查 |
| `hermes_forward` | 入口 `main.py` 已改名停用 | 开 provider 后必须停它，否则双回复 |
| `hermes_memo` | **常驻记忆块**（2026-09-23 新增，v1.0.0） | 每轮把 `/AstrBot/data/mianmian-memo/MEMORY.md` 追加进她的 `system_prompt`，她自己能用 `memo_view/memo_add/memo_replace/memo_remove` 改。源码副本 `chat-layer/plugin/hermes_memo/`、加载副本 `stack/astrbot/data/plugins/hermes_memo/`（**在 data 挂载里，重建不丢**），配置 `data/config/hermes_memo_config.json`。细节见下面「附三」+ 插件自己的 `README.md` |

---

## 附三：hermes_memo（聊天门「常驻记忆块」，2026-09-23 上线）

**为什么**：主人判断「留在 AstrBot、把 Hermes 值钱的机制搬过来」。AstrBot 侧缺的是**常驻上下文里的固定记忆块**
（= Hermes 的 `MEMORY.md`/`USER.md` 每轮都在系统提示里），Hindsight 的按查询召回给不了这种「一直都在」的底。

**Hermes 侧机制（读源码，只取机制不搬代码）**：`agent/system_prompt.py` 的 volatile 层把
`tools/memory_tool_store.py::MemoryStore` 渲染的块拼进系统提示词；条目用 `"\n§\n"` 分隔、有字符上限、
超限时拒绝写入并要求先合并；模型用 `memory(action=add/replace/remove)` 维护。

**AstrBot 侧落地**：新插件 `hermes_memo`
- 数据文件：`/AstrBot/data/mianmian-memo/MEMORY.md`（宿主 `stack/astrbot/data/mianmian-memo/MEMORY.md`，**755/644 已放开给主人读**）
- 注入点：官方钩子 `@filter.on_llm_request()`（`internal.py:352` 派发），**追加在 `req.system_prompt` 最尾部**
  → 人格/技能段稳定不破坏前缀缓存；每轮现读文件，改完下一条生效；同 req 幂等
- 工具：`memo_view` / `memo_add(content)` / `memo_replace(old_text, content)` / `memo_remove(old_text)`
  （AstrBot 读 docstring 的 `Args:` 段当参数表，改参数要连 docstring 一起改）；写权限默认只给主人私聊 umo
- 初始化：文件不存在时写 6 条初始条目（**只写一次**，之后只由她自己改）

**验证（三层，全过才算数；可重跑）**
```bash
# ① 类行为/纯逻辑（沙箱）   → 结论：自测全过（A~H 共 24 项）
cd /opt/data/scripts && bash astr_run.sh hermes_memo_selftest.py
# ② 真框架探针（真装饰器/真事件/真 call_event_hook/真工具 handler）→ 20/20 PASS
cd /opt/data/scripts && bash astr_run.sh hermes_memo_live_probe.py
# ③ 线上运行态（重启后真请求）
docker logs --since 10m astrbot | grep -a hermes_memo
```
③ 的实测证据（2026-09-23 14:00，真回执触发的那一轮）：
`[hermes_memo] 注入 n=6 chars=761 用量=18% — 463/2,500 字 umo=aiocqhttp:FriendMessage:<OWNER_QQ>`；
同轮 agent 的工具表从 41 个变 49 个（含 `memo_view/memo_add/memo_replace/memo_remove`）。
主人可感知：猫猫自己主动说了「我现在每轮眼前都自动挂着一块常驻记忆，不用去翻了 / 还给了 memo_add、memo_replace、memo_remove 三个工具让我自己维护」。

**探针坑（已写进脚本注释，复用时别踩）**：`FuncTool.handler` 是**导入时**就抓走的原始函数引用，
光按 hermes_memory 探针那样绑注册表元数据不够，`llm_tools.func_list` 里那份还是未绑定的 raw
（症状：`memo_add() missing 1 required positional argument: 'event'`）→ 必须照 `star_manager.py:1262-1305`
**两段都做**（注册表 + `llm_tools.func_list`）。

**回滚**：删 `data/plugins/hermes_memo/`（或 `data/config/hermes_memo_config.json` 里 `enable=false`）→ 重启 AstrBot。
记忆文件留着不影响任何东西（没人读它）。

---

## 附四：聊天门「单次查询」工具箱 + 工具边界（2026-09-23，我加的）

**目标**（主人设计意图）：把聊天门从「聊天员」升成「轻量助理」——能单次查询（搜网页 / 抓正文 / 查 B 站），
但**多轮·长活·登录态一律派给干活门**（她一旦开干就停不下来，中途改不了）。

### 新增插件 `hermes_lookup`

源码 `chat-layer/plugin/hermes_lookup/`（含 README + tests/verify_lookup.py）；
**compose 没挂它**，跟 `hermes_memory`/`hermes_memo` 一样住在数据卷 `data/plugins/hermes_lookup/` → 重启/重建都不丢。
同步：`sudo docker cp …/chat-layer/plugin/hermes_lookup/. astrbot:/AstrBot/data/plugins/hermes_lookup/` + `docker restart astrbot`。

5 个 LLM 工具（全部免登录、单次请求、硬超时）：

| 工具 | 通道 |
|---|---|
| `web_search(query, max_results)` | 自建 SearXNG `172.17.0.1:18888`（`format=json`） |
| `fetch_page(url, max_chars)` | 直连 → 失败走宿主代理 `172.17.0.1:17890` 一次；`mp.weixin.qq.com` 自动换微信内置浏览器 UA |
| `bilibili_lookup(target)` | `x/web-interface/view` + `x/player/v2`（标题/UP/时长/统计/简介/**官方分章**/字幕可用性） |
| `bilibili_search(keyword, max_results)` | `x/web-interface/search/type?search_type=video`（先取首页 buvid3 访客 cookie） |
| `recent_activity()` | 只读 `data_v4.db` 查「主人最近一次说话是多久前」（顶替她原来跑 python 查库那条路） |

配置 `data/config/hermes_lookup_config.json`（`enforce_boundary` / `owner_umo` / `trace_tools` …）；
自检日志前缀 `[hermes_lookup]`：启动打「已注册工具面」+「白名单稽核」，每轮打「边界执行」。

### 硬边界是**两层**（第二层少不了）

1. **人格白名单**：`personas.tools`（persona_id=`mianmian`）从 `null`（全给）改成显式闭集
   —— 2026-09-23 起为 **22 项**（含文件四件套，见附五）；
   写库脚本 `chat-layer/apply-persona-boundary.py`（幂等、改前自动备份到 `data/_persona_backup_<ts>.json`）。
2. **每轮剪枝**（`hermes_lookup.enforce_boundary`）：AstrBot 的 local runtime
   （`computer_use_runtime: local`）在 `astr_main_agent.py:1643 _apply_local_env_tools()` **每轮无条件**
   往请求里塞 `astrbot_execute_shell / astrbot_shell_session / astrbot_execute_python /
   astrbot_file_read_tool / astrbot_file_write_tool / astrbot_file_edit_tool / astrbot_grep_tool`，
   还往 `system_prompt` 加一段英文「你能跑 shell 和 Python」——**人格白名单挡不住它们**。
   插件在 `@filter.on_llm_request()` 里按白名单逐个 `remove_tool()`。
   那段英文提示**逐句校正**（2026-09-23 改，见附五）：删 shell/python 句、留 workspace 相对路径句
   —— 不能整段删（她要用 workspace 相对路径），也不能留着（她会以为自己能跑 shell）。

**实测（2026-09-23 14:07，主人私聊真轮；⚠️ 下面是换符前的历史快照，19 项）**：

```
[hermes_lookup] 边界执行：本轮摘掉 6 个白名单外工具: astrbot_execute_python,astrbot_execute_shell,
  astrbot_file_edit_tool,astrbot_file_write_tool,astrbot_grep_tool,astrbot_shell_session
[hermes_lookup] 本轮工具面(19): astrbot_file_read_tool,bilibili_lookup,bilibili_search,
  delegate_to_hermes,fetch_page,future_task,list_memories,llm_publish_feed,llm_view_feed,
  memo_add,memo_remove,memo_replace,memo_view,recall,recent_activity,reflect,retain,
  send_message_to_user,web_search
```

**端到端（14:10 真轮）**：她真的调了 `bilibili_search`（自己搜的「兰州牛肉面」），拿到结果后用自己的话报出来 ✓
（没贴原始列表、没报参数——人格里那条「用查询工具的规矩」生效）。

### ⚠️ 两条维护规矩

- **新增插件 LLM 工具必须加进白名单**（`chat-layer/apply-persona-boundary.py` 的 `ALLOW`）并重跑，
  否则她够不着（每轮被剪掉，只有一行日志）。启动日志的「白名单稽核」行会把漏掉的点名。
- 人格提示词里那段《工具与边界（硬规矩）》的正本在
  `chat-layer/plugin/hermes_lookup/persona-boundary-section.md`；写库脚本按
  「`## 派活` 或 `## 工具与边界…` → `## 群聊`」的范围整段替换 → **别手改库里那一段**，会被下次覆盖。
  （另有同名旧副本 `chat-layer/persona-boundary-section.md`，已同步成一样的内容，但**以 plugin 目录那份为准**。）

### 已知取舍（留给出题人拍板）

- 另一个会话的 `proactive-messaging` skill 第 2 步原来教她用 `astrbot_execute_python` 读库判断
  「30 分钟内聊过 → 不打扰」。她没有 python 了 → 该改用 `recent_activity()`（只读、语义等价）。
  **skill 里那句引用要换掉**（我没动 skills 目录，避免和另一个会话撞车）。
- 登录态网站（京东/B 站个人页/NAS 面板）**故意没给她接**：她那条链路的正确形状是「派给干活门」
  （干活门侧有 `fygo-browser-use` 连宿主 CDP）。她的容器是 host 网络，CDP `172.17.0.1:16003`
  够得着，但驱动 CDP 必然是多轮 + 需要看中间结果 → 违反边界，不做。

---

## 附五：分段符 `※` → `⁂` + 文件工具加回 + 提示词逐句校正（2026-09-23 15:1x）

**这不是补丁**（不改 `/AstrBot/` 源码），是**容器 data 目录里的配置 + 人格库改动**；
`/AstrBot/data` 是卷 → **重建不丢**，但换机器/换卷要照本节重做。

### ① 分段符：`※`(U+203B) → `⁂`(U+2042)

**动因**：`※` 在正常文档正文里也会出现（引用注记那种），会被切成多条；`⁂` 只作分段用。

源码依据：`/AstrBot/astrbot/core/pipeline/result_decorate/stage.py`
—— `:59-66` 从 `ctx.astrbot_config["platform_settings"]["segmented_reply"]` 读键；
`:229-245` `re.findall(regex, text, re.DOTALL|re.MULTILINE)` 切、再按 `content_cleanup_rule` 清。

改的东西（`/AstrBot/data/cmd_config.json` 的 `platform_settings.segmented_reply`）：

| 键 | 旧 | 新 |
|---|---|---|
| `regex` | `.*?[。？！~…※]+\|.+$` | `.*?[。？！~…⁂※⸮]+\|.+$` |
| `content_cleanup_rule` | `[※]` | `[⁂※⸮]` |

**容错集合 `⁂`/`※`/`⸮` 全留着** —— 模型万一沿用旧话术，也照样切、也不会发出来。

脚本（幂等、改前备份、写回后再读校验）：
```bash
SUDO='export SUDO_ASKPASS=/vol1/1000/<USER> sudo -A'
bash /opt/data/scripts/fygo_ssh.sh "$SUDO docker exec astrbot python3 /AstrBot/data/_apply_seg_symbol.py --dry"
bash /opt/data/scripts/fygo_ssh.sh "$SUDO docker exec astrbot python3 /AstrBot/data/_apply_seg_symbol.py"
```
- 源码正本：`chat-layer/apply-seg-symbol.py`（同步到 `/AstrBot/data/_apply_seg_symbol.py`）
- 备份：`/AstrBot/data/_cmd_config_backup_<ts>.json`
- 实测输出：`写回校验 OK：regex='.*?[。？！~…⁂※⸮]+|.+$' cleanup='[⁂※⸮]'`
  / `其余键未动：enable=True interval=1.5,3.5 threshold=150 split_mode=regex`
- 人格正文里另 6 处 `※` 也一并换成 `⁂`（由 `_apply_boundary.py` 顺带做，见下）

### ② 文件四件套加回白名单（**不加路径 allowlist**）

主人裁决（2026-09-23）：「文件类还给她」+「路径保险不要了，我在提示词里面写严了就够了」。

- `chat-layer/apply-persona-boundary.py` 的 `ALLOW` 加回
  `astrbot_file_read_tool` / `astrbot_file_write_tool` / `astrbot_file_edit_tool` / `astrbot_grep_tool`
  → 白名单 **19 → 22 项**；`astrbot_execute_shell` / `astrbot_shell_session` / `astrbot_execute_python`
  **仍然不给**，每轮照旧剪掉。
- **没有**任何路径 allowlist/沙箱 —— 边界只写在人格《碰文件的规矩》里
  （不碰系统与配置目录、别人的数据、记忆库；路径不熟先问；要跑命令/装东西/动服务 → 派活）。
- 同一脚本顺带做 `system_prompt` 的 `※`→`⁂`（本次 6 处）。

### ③ 那段英文提示改成**逐句校正**（`plugins/hermes_lookup/main.py`）

原逻辑：只在检测到 `"host local environment"` 时，把这些行整行删掉。
问题：文件工具回来了以后，AstrBot 注入的那段里
「`astrbot_file_read_tool` … resolve relative paths from it」是有用的，整段留会连
「你能跑 shell 和 Python」一起留着。

新逻辑（`enforce_boundary`）：按**本轮真实白名单**逐句处理 ——
`astrbot_execute_shell`/`astrbot_execute_python` 不在白名单 → 只删
「`astrbot_execute_shell` and `astrbot_execute_python` use it as their working directory.」那半句，
**留下 workspace + 文件四件套的相对路径提示**；再按行删掉 shell/python 的整行。
（原注入点：`astr_main_agent.py:1706-1722`）

### 验收（机器判定，可重跑）

| 判据 | 结果 |
|---|---|
| `_selftest_segmentation.py` —— **走真身 `ResultDecorateStage`** + 线上 `cmd_config.json` | **10/10**：含 `⁂` 的文本切成 3 条、`※`/`⸮` 同样切且不漏、无分隔符不切、换行不切、超 150 字整段不切 |
| `_selftest_boundary.py` —— **走真身 `enforce_boundary`** + 线上白名单 | **24/24**：文件四件套留着、shell/python/shell_session 摘掉、提示词里不再有 shell 句、workspace 句还在 |
| 启动日志 | `白名单稽核：人格白名单 22 项`；`补丁已生效 ✓`；`MCP services 1/1`；`aiocqhttp(OneBot v11) 适配器已连接。` |
| ERROR / Traceback（本次启动以来） | **0 条** |
| `python3 /opt/data/scripts/health_all.py` | **✅ 全部正常（29/29）**，其中 `persona mianmian tools_count=22` |

**每轮剪枝实测（重启后的真身自测输出，等价一轮真请求）**：
```
[hermes_lookup] 边界执行：提示词校正 shell/python 句
[hermes_lookup] 边界执行：本轮摘掉 3 个白名单外工具:
  astrbot_execute_python,astrbot_execute_shell,astrbot_shell_session
```

### 回退

见 `chat-layer/plugin/hermes_onebot/RUNBOOK.md` 第三节（两个备份目录 + 重启后的三件事核对清单）。

### 关联

- 人格提示词正本：`chat-layer/plugin/hermes_lookup/persona-boundary-section.md`
  （本次重写了《碰文件的规矩》第 6 条：删掉「越界会被挡」的沙箱说法，改成「这条线你自己守；
  主人明确否掉了路径保险」；《硬边界》第 1 条写明「干活门那边主人能随时喊停/改方向，你不能」）
- Hermes 侧同款改动：`profiles/chat/SOUL.md`（文件四件套 + 《碰文件的规矩》+ 派活理由）
- 方向清单：`chat-layer/CHAT-AGENT-DIRECTION.md` §三（闭集改为含文件四件套、砍 shell/解释器/子代理编排/运维类）

