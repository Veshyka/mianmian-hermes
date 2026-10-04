# AstrBot 源码补丁 —— 持久化与自动恢复

**一句话**：AstrBot 的源码在镜像层 `/AstrBot/`，只有 `/AstrBot/data` 是卷。所以源码补丁
**容器一重建就丢**，丢了会静默回到「本地工具报 `missing argument: 'context'` / `multiple values
for argument 'context'`」的坏状态。本目录把补丁做成**重建即自动重打**，不再依赖任何人的记忆。

补丁内容与根因见 `../astrbot-patches.md`（登记正本）。

## 目录里有什么

| 文件 | 作用 |
|---|---|
| `patch_astrbot_context.py` | **合并版补丁（幂等）**：一次覆盖全部 5 处改动。重建后唯一需要的重打入口 |
| `verify_patch.py` | **机器判定自检**：AST 静态分析 5 条判据，exit 0 = 补丁生效（可当重建后的自动验收） |
| `astrbot-entrypoint.sh` | **容器启动入口**：先打补丁 + 自检，再 `exec` 镜像原 CMD（`python main.py`） |
| `apply-from-host.sh` | **宿主侧一键补打**：不依赖容器怎么被建出来的，兜底用 |
| `legacy-patch_astrbot_context_v3.py` | 历史分步脚本（**只修 3 处，单独跑会坏**），仅留档，别再用 |

## 三道防线

1. **自动（主）**：`chat-layer/docker-compose.yml` 里 astrbot 服务的
   `entrypoint: ["/bin/bash", "/opt/astrbot-patches/astrbot-entrypoint.sh"]`
   + 只读挂载 `./patches → /opt/astrbot-patches`。
   任何 `docker compose up -d` / `--force-recreate` / 换镜像后的重建，python 起之前都会自动重打
   （幂等：已是目标状态就跳过，不产生多余备份）。
2. **一键（兜底）**：容器不是 compose 建的（手搓 `docker run`、导入镜像、别的脚本）时，在宿主执行
   ```bash
   bash /vol1/1000/<USER>          # 打补丁+自检+重启
   bash /vol1/1000/<USER> astrbot --no-restart
   ```
3. **可查**：每次启动的补丁结果写在容器内 `/AstrBot/data/patches-boot.log`（在卷里，重建不丢），
   `docker exec astrbot tail -20 /AstrBot/data/patches-boot.log` 即可看最近一次是「跳过」还是「已改」。

## 怎么验证（机器判定，别靠眼看）

**① 全新容器模拟重建**（等价于「重建后能不能自愈」，不碰线上容器）：

```bash
# 在宿主执行
docker run --rm -v /vol1/1000/<USER> \
  -v /tmp/astrbot-sim-data:/AstrBot/data -e PATCH_ONLY=1 \
  --entrypoint /bin/bash soulter/astrbot:latest /opt/astrbot-patches/astrbot-entrypoint.sh
# 期望尾行：[VERIFY] 补丁已生效 ✓
```

**② 线上容器现状**：`docker exec astrbot python3 /opt/astrbot-patches/verify_patch.py` → `[VERIFY] 补丁已生效 ✓`

**③ 真链路（不写记忆）**：跑 `stack/astrbot/data/mianmian-tmp/verify_tools.py local|file`
——`local` 走「handler 型本地工具 + 参数里带 `context`」这条崩溃路径，`file` 走真内置文件工具；
两者都返回正常内容即链路通。**别跑 `retain` 模式**（会往 Hindsight 写真记忆，产生测试残留）。

## 已知边界

- 补丁打的是 `/AstrBot/` 里的文件：**镜像升级后若上游重写了这几处，锚点断言会失败**（脚本报
  「锚点命中 0 次」，启动不中断但会记 `!!! 补丁自检未通过 !!!`）→ 这时按 `../astrbot-patches.md`
  重新定位行号与锚点，别硬套。
- entrypoint 覆盖的是**容器启动命令**（镜像自身 `ENTRYPOINT=null`、`CMD=["python","main.py"]`，
  脚本用 `exec "$@"` 承接原 CMD）。若上游把启动命令改到 entrypoint 里，需要同步本脚本。
- 打补丁失败**不阻止启动**（宁可少功能也不要聊天门 down），失败只记日志 —— 所以排查时以
  `verify_patch.py` 的退出码为准，不要以「容器起来了」为准。
