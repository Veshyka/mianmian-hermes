# Tailscale 落地记录（2026-08-25 完成）

飞牛套件版 Tailscale 部署完成态，供后续排障/扩展参考。

## 关键路径

- 二进制：`/vol2/@appcenter/tailscale/bin/tailscale`
- socket：`/vol2/@appdata/tailscale/tailscaled.sock`（**所有 tailscale 命令必须带 `--socket=`**，否则找不到实例）
- 日志：`/var/apps/tailscale/var/tailscale.log`

## 授权流程（未授权状态排查）

```
tailscale --socket=... up --hostname=SHY-2 --accept-routes=true --accept-dns=false --ssh
# 未授权输出: Logged out. Log in at: https://login.tailscale.com/a/<device-code>
# 已上线输出: 100.73.132.15  shy-2  <OWNER_QQ>@  linux  -
```

- 授权 = 浏览器打开 `login.tailscale.com/a/<code>`，用主人 Tailscale 账号登录确认设备
- 主人看到的面板 ID（如 Tt2t4Aiozp11CNTRL）≠ 授权链接 code，别混淆
- 验证：`tailscale --socket=... status`（Logged out / NeedsLogin = 没授权成功）

## 子路由（访问整个内网）

```bash
tailscale --socket=... set --advertise-routes=192.168.123.0/24
```

- 广播后**必须管理后台批准**才生效：https://login.tailscale.com/admin/machines → 点设备 → Edit route settings → 勾选子网 → Save
- 批准前只能访问 NAS 本身（100.73.132.15 的各端口），批准后可访问整个 192.168.123.0/24

## 已部署状态

- 设备名：shy-2，Tailscale IP **100.73.132.15**（ULA fd7a:115c:a1e0::1132:8410）
- 子路由 192.168.123.0/24 已广播 + 已批准
- 路由器 IPv6 已开（Native DHCPv6，240e 段）→ Tailscale 双端 IPv6 直连基础就绪
- Hermes dashboard：宿主 19119 → 容器 9119（有登录鉴权）；远程入口 `http://100.73.132.15:19119`

## DERP 延迟数字解读

- 客户端显示的 DERP 节点延迟（旧金山 189ms 等）= **中继兜底 ping**，非实际连接速度
- Tailscale 在中国无官方 DERP 节点，国外节点延迟高属正常
- IPv6 直连后流量不走 DERP，该数字是摆设；`tailscale ping <设备>` 看 direct（直连）还是 relay（中继）
- **但「摆设」只成立于打洞成功时**：实测本机国际下行极差（东京下行 1.63 Mbps / 上行 43.71 Mbps），回落 DERP 后吞吐会塌到个位数 Mbps。**组网流畅度的第一判据是「有没有直连」，不是 DERP 延迟**；`status --json` 里 Peer 的 `CurAddr` 非空即直连，为空而 `Relay` 有值即中继。
- 所有 peer 离线时 `tailscale ping` 一律 timeout、`CurAddr` 全空 —— 这是**没有对端**，不是链路故障，别误判成打洞失败。测速前先确认至少一个 peer online。
- 量吞吐的完整做法见本 skill `references/throughput-testing.md`。

## iOS 客户端

- 国区 App Store 无 Tailscale（下架）→ 外区 Apple ID 下载
- iOS 单 VPN 隧道限制：Tailscale 与梯子 App 不能同时开；场景天然分离（翻墙 vs 连家里），手动切换
- 分流模式：只路由 Tailscale 网段，日常上网不绕隧道；待机耗电可忽略
