# 平台凭证迁移清单与适配器配置键速查

迁移来源示例：旧框架配置（OpenClaw `openclaw.json` 的 `channels.<platform>.*`：appId/clientSecret/token）→ Hermes `config.yaml` / `.env`。**密钥全程不打印**：python 读旧配置直接写入目标，验证输出注意脱敏（`hermes config get` 会回显完整值）。旧框架原件保留在交接包目录里，可逆。

## QQ Bot（Hermes 原生支持，最顺）

| 旧框架键 | Hermes 位置 |
|---|---|
| `channels.qqbot.appId` | `platforms.qqbot.extra.app_id`（或 env `QQ_APP_ID`） |
| `channels.qqbot.clientSecret` | `platforms.qqbot.extra.client_secret`（或 env `QQ_CLIENT_SECRET`） |
| `channels.qqbot.allowFrom` | `platforms.qqbot.extra.allow_from`（dm_policy=allowlist 时） |
| `channels.qqbot.groups` | `platforms.qqbot.extra.group_policy` / `group_allow_from` |

- 默认 `dm_policy: pairing`；`open` 必须配 `.env` `QQ_ALLOW_ALL_USERS=true`（安全闸，否则 gateway 拒启）
- 主人 openid 需首条消息后确认（用户+应用维度；旧框架的 c2c ID 是否可复用实测为准）
- 适配器源码：`/opt/hermes/gateway/platforms/qqbot/adapter.py`（配置键：dm_policy / allow_from / group_policy / group_allow_from）

## 微信（iLink）

- Hermes 适配器 `gateway/platforms/weixin.py` 需要 `WEIXIN_TOKEN` + `WEIXIN_ACCOUNT_ID`（iLink Bot API：长轮询 getupdates + QR 登录）
- 旧框架侧若只有 `enabled: true`、没有凭证（credentials 目录也没有），就是**无凭证**——不接入，别当缺陷排查
- 相关键：`WEIXIN_BASE_URL`（默认 iLink）、`WEIXIN_DM_POLICY`、`WEIXIN_GROUP_POLICY`、`WEIXIN_ALLOWED_USERS`、`WEIXIN_GROUP_ALLOWED_USERS`

## 钉钉

- 无凭证时启动报 `config validation failed` / `adapter creation failed` → `hermes config set platforms.dingtalk.enabled false` 关掉，避免每次启动刷错误

## 通用流程

1. `hermes config set platforms.<name>.extra.<key> <value>`（官方 CLI 原子写入，嵌套键支持）
2. 环境变量类放 `/opt/data/.env`（600 权限）
3. 重启 gateway 生效：`hermes gateway run --no-supervise --force`（新进程）
4. 验证：gateway 日志 `✓ <platform> connected`；`hermes send --list` 看目标
5. 拿不到用户身份就先让对方发一条消息，再从日志/适配器记录里取 openid，然后收紧 allowlist
