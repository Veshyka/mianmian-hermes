# hermes_debounce —— 连发消息合并

AstrBot 4.28.1 插件：私聊里主人连发多条消息时，等一小段静默窗口，把这批消息**合并成一轮**交给模型，只回一次。

## 行为

1. 主人发第一条 → 开始倒计时（`wait_seconds`，默认 7 秒）
2. 窗口内每来一条新消息 → **重新计时**
3. 静默窗口结束 → 这几条按顺序用 `\n` 拼成一条，作为**这一轮**的 prompt 交给模型（模型只回一次）
4. 硬上限 `max_wait_seconds`（默认 45 秒）：从本轮第一条算起，到点**强制放行**，不会无限等
5. `scope=private`（默认）只在私聊生效；`scope=both` 时群聊也合并，按「会话+发送者」分别攒（不同人的话不会混成一轮）

## 配置（WebUI 插件配置里改，或改 `data/config/hermes_debounce_config.json`）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 总开关，关掉就恢复「一条一回」 |
| `wait_seconds` | int | `7` | 静默窗口（秒）；来新消息就重新计时 |
| `max_wait_seconds` | int | `45` | 硬上限（秒）；绝不突破，到点强制放行 |
| `scope` | string | `private` | `private` / `both` |

改完配置在 WebUI 里重载插件（或重启容器）生效。

## 实现要点（源码依据）

- 钩子：`@filter.event_message_type(EventMessageType.ALL, priority=100)`（`priority` 越大越先跑，见 `core/star/star_handler.py:22-29`）
- 每条消息都是独立 task（`core/event_bus.py:54`），所以「leader 在自己的处理函数里 await 窗口」不会卡住后续消息的接收
- 窗口期内的后续消息：塞进本轮后 `event.stop_event()`；洋葱模型在 `core/pipeline/scheduler.py:59-63` 按 `is_stopped()` 断链，后面几个 stage（含模型调用、发送）都不会跑
- 模型 prompt 就是 `event.message_str`（`core/astr_main_agent.py:1367`），leader 把合并文本写回这个字段即可
- 指令（`/` 开头、唤醒前缀开头）一律不拦，照常立刻执行
- 群聊里没点到猫猫的消息（无活跃轮次时）完全不碰；同一个人 @ 之后接着补的无 @ 消息会并进本轮
- 日志只打条数与长度，绝不打消息正文

## 自测

```bash
# 纯逻辑测试（不需要 astrbot，宿主/Hermes 容器里都能跑）
python3 tests/test_debounce.py            # → 47 项断言全过

# 容器内集成自测（直接驱动真实插件 handler，假 event，不发任何消息）
sudo -A docker cp tests/test_debounce_integration.py astrbot:/tmp/
sudo -A docker exec astrbot python3 /tmp/test_debounce_integration.py   # → 34 项断言全过
```

留档输出：`tests/offline_test_output.txt`、`tests/container_integration_output.txt`
启动日志留档：`tests/astrbot_boot_final.log`

## 主人自己验一遍（30 秒）

私聊里连发 3 条不相关的短句（例如「在吗」「今天忙不忙」「帮我看下机器」），然后：

```bash
sudo -A docker logs --since 2m astrbot | grep hermes_debounce
```

应看到一条 `放行一轮 原始3条 合并后len=… 等待…s`，且猫猫**只回一次**（回的内容是针对三条合起来说的）。
