# 数据迁移 + 镜像更新实战（2026-08-25）

## 迁移：/vol2/docker/hermes-data → /vol1/1000/<USER>

主人指定共享文件夹统一映射。**官方文档确认**（hermes-agent docs /user-guide/docker）：容器所有用户数据在挂载的 `/opt/data` 单目录，镜像无状态，pull 新镜像重建不丢配置。

### 步骤（在线迁移，不停容器）
```bash
# 1. 建目标目录 + rsync 复制（-a 保留权限；--exclude 运行时日志）
sudo mkdir -p /vol1/1000/<USER>
sudo rsync -a /vol2/docker/hermes-data/ /vol1/1000/<USER>
# 2. 校验：文件数对比（运行时文件差几个正常）+ 关键资产逐一 test -f
#    .env / config.yaml / SOUL.md / gateway_state.json / skills/ / scripts/ / cron/
# 3. 改重建脚本挂载行（sed 替换），nohup 宿主侧跑（见 SKILL.md 自杀陷阱）
# 4. 验证：mount 显示 subvol=/1000、gateway 槽 up、QQ/微信 connected
```

### 回滚点（保留未删）
- 旧数据目录 `/vol2/docker/hermes-data`（原样）
- 保底容器 hermes-old 系列（确认正常后 `docker rm` 清理）

## 镜像更新（主人手动 pull + 重建）

- 更新后验证：`/opt/hermes/bin/hermes --version`（upstream hash 变化 = 新版）；挂载仍指向 mianmian
- **重建脚本位置**：`/opt/data/scripts/hermes-rebuild-gateway-mode.sh`（容器内 = 新挂载目录下）
- 更新后全量验证：gateway 槽 up、gateway_state.json（qqbot/weixin connected）、dashboard 9119→19119 302、skills 数量、state.db 大小

## 教训

1. **docker stop hermes = 自杀**（容器内跑 Hermes）——重建必须宿主侧 nohup 脚本，别前台跑
2. **重建期间主人手动启动会和脚本打架**——反复启停混乱，先跟主人打招呼
3. 迁移后验证要「从容器内视角」测端口（容器内 9119，宿主 19119，别搞混）
