# 迁移残留：990 属主目录（2026-09-19 实测）

容器 uid 在迁移时从 **990（旧 OpenClaw 应用用户 trim.openclaw:AppUsers）** 变成 **1003（hermes）**，但一批顶层目录还归 990，且部分权限很紧。

| 目录 | 属主:权限 | 影响 |
|---|---|---|
| `cache/` | 990:901 `755` | **`cache/vision` 建不了 → 识图整条挂掉**（`Native vision failed: Permission denied: '/opt/data/cache/vision'`）；`cache/images` 同理 |
| `image_cache/`、`audio_cache/` | 990:901 `600` | 我连读都读不了，图片/音频缓存不可用 |
| `backups/` | 990:901 `755` | 只有 `backups/config` 归 hermes（早前处理过） |
| `bin/`、`pasts/`、`photon/`、`platforms/`、`plugins/`、`sandboxes/` | 990:901 `755` | 目前读得到（Hermes 跑得动），但**写会失败**（如装插件） |

## 处置（2026-09-19 已根治）

**根治**：把 14 个确认属于 Hermes 自身的残留（`cache` `image_cache` `audio_cache` `bin` `plugins` `sandboxes` `photon` `platforms` `pastes` + `install_id` `projects.db` `state.db.fts_rebuild.lock` + 两个交接文档）`chown` 成 `1003:1001`。非 hermes 条目 33 → 6（只剩 `backups/` 与一个 root 属主 zip，均有意保留）。

**全部登记在 `/opt/data/ops-changelog/ownership-changes.md`**（含理由、逐条回滚命令、验证结果）。主人授权口径：**先确认文件不属于主人** → 可以 chown → **必须登记**。

**临时手段（已撤）**：动手前用过 Hermes 自带的 legacy 回退顶过一阵——

```bash
mkdir -p /opt/data/temp_vision_images && touch /opt/data/temp_vision_images/.keep
# get_hermes_dir('cache/vision','temp_vision_images') → 旧名目录只要"有内容"就会被优先使用
```

`get_hermes_dir(new_subpath, old_name)`（`/opt/hermes/hermes_constants.py`）的规则就是：**旧名目录只要有内容就用旧名目录**，否则用新子路径。副作用是永久遮蔽新路径（官方 docstring 专门警告过这种"空壳遮蔽"），所以属主修好后就该撤掉。

## 排查这类问题的固定顺序

1. 报错带 `Permission denied: '<HERMES_HOME>/...'` → 先 `ls -ld` 那个目录看属主
2. 属主是 **990** → 这是迁移残留，不是配置或代码问题
3. **处置口径（主人 2026-09-19 授权）**：先确认文件**不属于主人**（是 Hermes 自身缓存/运行态/棉棉自己写的文档，而不是主人的数据）→ 可以 `chown 1003:1001` → **必须登记进 `/opt/data/ops-changelog/ownership-changes.md`**（理由 + 回滚命令 + 验证结果）。属于主人的数据（如 `backups/`）不动，只报告
4. 属主是 **hermes(1003)** 却仍失败 → 那才是真的配置或代码问题
5. 一时改不了（主人不在场 / 不便 sudo）可以先绕：见上面的 legacy 回退；能绕就别卡住
