# 棉棉 · 本地 Hermes 定制层（自建 AI 助手的一整套本地化改造）

> **English blurb**: A collection of locally-written plugins, skills, scripts and engineering
> notes built on top of [Hermes Agent](https://hermes-agent.nousresearch.com/) — a self-hosted
> AI assistant running on a home NAS. Includes a OneBot11 (QQ) platform adapter with group
> wake-up scoring / proactive messaging / sticker auto-tagging, a Hindsight memory plugin,
> watchdog & health-check scripts, and the changelog that records *why* each change was made.
> All personal data, credentials and persona files are stripped — see `SECURITY.md`.

---

## 这个仓库是什么

一台家用 NAS 上跑着一个自建的 Hermes Agent（代号「棉棉」），连着 QQ、微信、本地语音栈、
本地视觉/向量模型、媒体库自动化等等。这个仓库是**围绕它自己写的那些定制件**和工程记录 ——
不是 Hermes 本身，也不是任何私密数据。

上传它有三个目的（也是选材标准）：

1. **可读** —— 别人（或别的 AI）想知道「一个人是怎么把自建 AI 助手跑起来的」，能照着看；
2. **异地副本** —— 代码与文档的一份云端拷贝（凭证/记忆/人设不上传，所以它**不是**完整灾备）；
3. **可复用** —— 有用的部分别人能直接拿走，有问题能提回来。

## 目录

| 目录 | 内容 |
|---|---|
| `profiles/chat/plugins/onebot/` | **核心**：为 Hermes 写的 OneBot11（QQ）平台适配器 —— 群唤醒打分、主动私聊起头（斜坡+静默窗）、上下线补投、语音转写、表情包自动打标与按情绪出图、群管理动作、离线补投、健康自检与告警。含完整测试 |
| `plugins/hindsight_flush/` | Hindsight 记忆 provider 插件：进度退出时把缓冲的最后一轮冲进记忆库 |
| `scripts/` | 健康自检（25 项）、换镜像自愈看门狗、框架补丁重放、发布前脱敏/复核工具 |
| `skills/` | 技术类技能包（容器运维、本地 LLM、记忆引擎、媒体库自动化、网络、qBittorrent…） |
| `ops-changelog/` | **工程日志**：每条改动的触发、根因、修法、验证证据、回滚方式。比代码本身值钱 |
| `config.yaml`、`profiles/chat/config.yaml` | 脱敏后的配置样例（号码/令牌/路径都换成占位符） |

## 发布前怎么保证不泄漏

两条闸，都在本地跑，**任何一条不过就不许推**：

```bash
# ① 白名单拷贝 + 脱敏（只在 /opt/data 里跑，产物在 publish/repo）
python3 scripts/publish_prepare.py
# ② 独立复核：把本机真实凭证读出来，去产物里找；找不到才算过
python3 scripts/publish_verify.py
```

`publish_prepare.py` 是**白名单制**：只有脚本里显式列出的路径会被拷贝（「除了敏感的其余都传」
这种黑名单思路实测必漏）；`publish_verify.py` 不看它的自我报告，而是独立把本机真实凭证值
（协议端 token、sudo 口令、推送 token、API key、各类号码、真名昵称）读出来，逐个在产物树里找。

## 已知边界

* 只包含「技术可公开」的部分：**不含**记忆库、人设、私聊/群聊内容、群友信息、媒体文件、任何凭证；
* 脱敏是文本级替换（`<OWNER_QQ>`、`<TOKEN>`、`<SECRET>`、`/vol1/1000/<USER>` 之类），
  所以配置样例能看懂结构但跑不起来 —— 这是有意的；
* 部分脚本里的内网地址（`172.17.0.1`、`127.0.0.1`）保留原样：那是 docker 默认网段，不构成泄漏。

## 许可

见 `LICENSE`。
