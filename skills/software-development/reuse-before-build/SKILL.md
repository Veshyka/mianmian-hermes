---
name: reuse-before-build
description: Use BEFORE 写新脚本/工具/清理逻辑或改配置。先搜 Skills Hub 与官方插件。
---

# 先找现成的，再造轮子

**这条不是为了省事，是因为反复在这里翻车。** 规则本身一直写在 SOUL 里，但它是「文本规则」：要我先想起来才会执行。而技能清单是**每轮被机械扫描**的——所以把规则做成技能（带强触发描述），比写在长文件里的一行字可靠得多。

## 触发场景（满足任一就执行下面的清单）

- 要写新脚本 / 新工具 / 新自动化
- 要做「清理」「删除」「归档」「保留 N 个」这类策略
- 要改配置、加 cron、动权限
- 心里冒出「我写个脚本搞定」这句话的瞬间 ← **最重要的信号**

## 硬性清单（按顺序，别跳）

1. **搜 Skills Hub**
   ```bash
   hermes skills search <关键词>        # 换着来：cleanup / trash / prune / backup / retention
   hermes skills inspect <名字>          # 看内容再决定装不装
   ```
   注意：Hub 技能**不在**每轮看到的技能清单里（那里只有已装的 ~126 个），**不搜就一定看不见**。
2. **查官方内置插件**
   ```bash
   hermes plugins list                  # bundled 插件默认全是 not enabled
   hermes plugins enable <name>         # 官方方案优先
   ```
   已知官方现成能力：`disk-cleanup`（自动追踪并清 test_/tmp_/cron 产物，含 `/disk-cleanup status|dry-run|quick|deep`）。
3. **查官方文档**：Hermes 自己的事先看 https://hermes-agent.nousresearch.com/docs （`llms.txt` 有全量目录），别凭记忆说「Hermes 不支持」。
4. 都没有 → 才自己写；写完把本机踩的坑补回对应 skill。

## 先找「官方扩展点」——尤其接新平台/新能力（2026-09-23）

「Hermes 不支持 X」这句话在动手前必须用源码/文档否证一次。实测两例：

- **要接一个聊天平台，不用另起进程**：Hermes 的平台适配器本身就是**插件形态** —— 插件目录 `$HERMES_HOME/plugins/<name>/`（注意是**该 profile 的**目录，不是顶层）、`config.yaml` 的 `plugins.enabled` 显式开启 + `platforms.<name>.enabled: true`，插件里 `register(ctx)` 调 `ctx.register_platform(...)`。**只 import 公开模块、不改框架源码** → 不装系统依赖、不新增常驻进程、住在数据卷里 → **升级/重建都不会被吃掉**。反面（同样重要）：改容器内源码的补丁式做法，升级/重建必丢，得配自愈入口 + 登记。
- **先搜关键字再下结论**：`grep -rni "<能力名>" /opt/hermes` 命中 0 ≠ 不支持，可能只是没在文档里叫这个名字；官方文档（`llms.txt` 有全量目录）也要搜一遍。

判据一句话：**能靠官方扩展点（插件/配置/内置机制）实现的，不写补丁、不自建守护进程**。

## 已翻过的车（别重演）

| 情况 | 我做的 | 本来该用 |
|---|---|---|
| 清理老备份 | 自建 `.trash` 目录 + 自研回收站逻辑，还改了宿主目录属主 | Hub 的 **trash-cli**（`trash-put` / `trash-empty 7` / `trash-restore`）；官方 `disk-cleanup` 插件当时没开 |
| 搜索链路时好时坏 | 先后端切到公共中继（外挂） | 自己把 searxng 引擎池修好，见 `mihomo-airport-ops` |

## 验证方式（可检查，不靠自述）

- `hermes curator status` / `/opt/data/skills/.usage.json`：看这个 skill 的 `view_count` 有没有涨 → 证明开工前真的 load 了它
- 写脚本前手上应有「搜过 Hub」的实际输出（`hermes skills search` 的返回），而不是一句「我觉得没有」
