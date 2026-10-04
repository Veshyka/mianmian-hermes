# 脱敏规则目录（可直接抄的形态）

本机实现：`/opt/data/scripts/publish_prepare.py`（静态规则 + 动态真值）与 `publish_verify.py`（独立复核）。
规则列表按下面顺序跑，**先具体后泛化**，否则泛化规则会吃掉具体值、占位符变得没信息量。

## 1. 静态规则

| 类别 | 形态 | 说明 |
|---|---|---|
| 具体号码 | `(?<!\d)<OWNER_QQ>(?!\d)` → `<OWNER_QQ>` | 主人号 / 机器人号 / 主群号 / 申请者号 / app_id 各一条，**别用 `\b`** |
| 掩码形态 | `\b\d{3}\*{3}\d{2}\b` → `<GROUP_ID>` | 日志里常见的 `<GROUP_ID>` |
| Bearer | `(?i)\bBearer\s+[A-Za-z0-9._\-]{6,}` → `Bearer <TOKEN>` | HTTP 头 |
| 键值凭证 | `(?i)("?(?:token\|access_token\|secret\|api_?key\|app_?secret\|password\|passwd)"?\s*[:=]\s*)"?[A-Za-z0-9._\-/+]{6,}"?` → `\1"<SECRET>"` | 引号形态一起吃掉 |
| 截断密钥 | `\bsk-[A-Za-z0-9]{2,}[^A-Za-z0-9\n]{1,6}[A-Za-z0-9]{2,}` → `sk-<KEY>` | 覆盖 `sk-<KEY>`（含 unicode 省略号） |
| 完整密钥 | `\bsk-[A-Za-z0-9]{20,}` → `sk-<KEY>` | 门限 20 位，避开 `sk-cleanup` 这类路径片段 |
| 订阅链接 | `(?:https?\|vless\|vmess\|trojan\|ss\|hysteria2?)://[^\s"'<>]*(?:sub\|subscribe\|token=\|clash\|v2ray\|vless)[^\s"'<>]*` → `<SUBSCRIPTION_URL>` | 必须带 scheme，否则误伤 `subprocess` |
| 真名/昵称 | 字面替换 → `<OWNER>` / `<OWNER_NICK>` | 包括群昵称 |
| 会话 id | dm/群 chat_id（平台给的哈希串）→ `<DM_CHAT_ID>` | |
| 本机路径 | `/vol[12]/1000/[^\s`"')，。、；]*` → `/vol1/1000/<USER>` | 用户名可能是中文，字符类放宽；末尾什么都没有也要匹配（用 `*` 不用 `+`） |
| 群名 | 字面 → `<GROUP_NAME>` | |

## 2. 动态真值（第二遍）

去源头读出真实值再替换成 `<SECRET>`：

- 协议端配置 `onebot11_*.json` 里的 `"token"`
- askpass / sudo 脚本里的口令（`echo <passwd>` 那一行）
- `secrets/*.token`
- 各 `config.yaml` / `config.json` 里的 `api_key|token|secret|app_secret|password`

跳过集合：`not-needed`、`placeholder`、`changeme`、`none`、`dummy`、`test`、`***`、`your-key`、
长度 < 8 的值、纯数字（号码类交给静态规则，占位符更有信息量）。

同一份 `real_secrets()` 最好被生成器和复核脚本共用（一个实现，避免两份漂移）。

## 3. 复核模式（产物里不许出现）

- `\b1[0-9]{8,10}\b`（减去 `BENIGN_NUMBERS` 白名单）
- 号码出现在 qq/群 语境：`(?i)(?:qq|uin|群号|user_id|sender_id|group_id)["'\s:=]{0,6}(1[0-9]{8,10})`
- 未脱敏的 `Bearer` / `sk-` / 键值凭证 / 订阅链接 / 真名昵称 / `/vol[12]/1000/`
- 不该出现的路径：`memories`、`/logs/`、`secrets`、`state.db`、`.env`、`*-state.json`、`*-proactive.json`、
  `*-backfill.json`、`*-alerts.log`、`jobs.json`、`askpass`、`*-stickers`、`.bak`、`.venv`、`__pycache__`
- 二进制混入：`.png/.jpg/.db/.zip/.pdf/.gguf/.so` 出现在产物里就是 bug

`BENIGN_NUMBERS` 每条都要带一句出处（`epoch = <日期>`、平台 `message_id`、`测试夹具`），
否则下一个看报告的人分不清「核实过」和「懒得看」。

## 4. 每条命中的核实顺序

1. 在**源文件**里看上下文（不是产物）——`sed -n '<行>p'`，判断是时间戳/夹具/真号码；
2. 真号码 → 加进静态规则；良性值 → 加进 `BENIGN_NUMBERS` 并写出处；
3. 从不把未核实的命中「放过」——闸的价值就在于每次都归零。
